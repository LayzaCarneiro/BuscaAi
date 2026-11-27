"""Camada conversacional stateless da Etapa 10: limites, sanitização de histórico e resposta PT-BR determinística."""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from buscai.api.errors import APIError
from buscai.api.schemas import (
    ChatConversationContextSchema,
    ChatHistoryMessage,
    ChatRequest,
    ChatResponse,
    SearchResponseSchema,
    build_search_response,
)
from buscai.config import Settings
from buscai.orchestration.state import ExecutionStatus, OrchestrationResult
from buscai.pricing import CurrencyConverter

_URL_PATTERN = re.compile(r"(?:https?://\S+|www\.\S+|\bebay\.com/\S+)", re.IGNORECASE)
_EBAY_ITEM_ID_PATTERN = re.compile(r"\bv1\|\d+\|\d+\b|\bitem[-_]?\d+\b", re.IGNORECASE)
_CURRENCY_PRICE_PATTERN = re.compile(
    r"(?:US\$|R\$|USD|BRL|EUR|GBP|\$|€|£)\s*\d[\d.,]*|\b\d[\d.,]*\s*(?:USD|BRL|dólares)\b",
    re.IGNORECASE,
)
_ASSISTANT_LISTING_SEGMENT_PATTERN = re.compile(
    r"(?:\bdestaque\s*#\d+|\bprincipais?\s+(?:opções|resultados)\b|\bvendedor\b|\bseller\b|"
    r"\bfeedback\b|\boferta\s*#\d+|\bfrete\b|\bshipping\b|\bitem_id\b|\blink\s*:|"
    r"\banúncio\b|\bproduto\s*#\d+|\b(?:score|pontuação)\s*:)",
    re.IGNORECASE,
)
_LIST_ITEM_PREFIX_PATTERN = re.compile(r"^\s*(?:#\d+|[-*•]|\d+[.)])\s+")
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[\n.!?])\s+")


def validate_chat_limits(payload: ChatRequest, *, settings: Settings) -> int:
    """Valida limites configuráveis do chat e retorna o limite efetivo de produtos."""
    if len(payload.message) > settings.chat_max_message_chars:
        raise APIError(
            422,
            code="validation_error",
            message=f"message excede o limite configurado ({settings.chat_max_message_chars} caracteres).",
            details=[
                {
                    "field": "body.message",
                    "type": "string_too_long",
                    "message": f"Máximo permitido: {settings.chat_max_message_chars} caracteres.",
                }
            ],
        )

    if len(payload.conversation_history) > settings.chat_max_history_messages:
        raise APIError(
            422,
            code="validation_error",
            message=(
                f"conversation_history excede o máximo configurado ({settings.chat_max_history_messages} mensagens)."
            ),
            details=[
                {
                    "field": "body.conversation_history",
                    "type": "too_long",
                    "message": f"Máximo permitido: {settings.chat_max_history_messages} mensagens.",
                }
            ],
        )

    total_chars = 0
    for idx, turn in enumerate(payload.conversation_history):
        turn_len = len(turn.content)
        total_chars += turn_len
        if turn_len > settings.chat_max_message_chars:
            raise APIError(
                422,
                code="validation_error",
                message=(
                    f"Mensagem histórica #{idx + 1} excede o máximo configurado "
                    f"({settings.chat_max_message_chars} caracteres)."
                ),
                details=[
                    {
                        "field": f"body.conversation_history.{idx}.content",
                        "type": "string_too_long",
                        "message": f"Máximo permitido: {settings.chat_max_message_chars} caracteres.",
                    }
                ],
            )

    if total_chars > settings.chat_max_history_total_chars:
        raise APIError(
            422,
            code="validation_error",
            message=(
                f"conversation_history excede o limite total configurado "
                f"({settings.chat_max_history_total_chars} caracteres)."
            ),
            details=[
                {
                    "field": "body.conversation_history",
                    "type": "too_long",
                    "message": f"Máximo total permitido: {settings.chat_max_history_total_chars} caracteres.",
                }
            ],
        )

    effective_limit = payload.limit if payload.limit is not None else settings.search_default_limit
    if effective_limit > settings.search_max_limit:
        raise APIError(
            422,
            code="validation_error",
            message=f"limit excede o máximo configurado ({settings.search_max_limit}).",
            details=[
                {
                    "field": "body.limit",
                    "type": "less_than_equal",
                    "message": f"Valor máximo permitido: {settings.search_max_limit}.",
                }
            ],
        )
    return effective_limit


def _strip_listing_tokens(text: str) -> str:
    cleaned = _URL_PATTERN.sub(" ", text)
    cleaned = _EBAY_ITEM_ID_PATTERN.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _sanitize_assistant_turn(content: str) -> str | None:
    """Remove referências a títulos, URLs, preços, IDs e vendedores de turnos anteriores."""
    segments = [seg.strip() for seg in _SENTENCE_SPLIT_PATTERN.split(content) if seg.strip()]
    safe_segments: list[str] = []
    for seg in segments:
        if (
            _URL_PATTERN.search(seg)
            or _EBAY_ITEM_ID_PATTERN.search(seg)
            or _CURRENCY_PRICE_PATTERN.search(seg)
            or _ASSISTANT_LISTING_SEGMENT_PATTERN.search(seg)
            or _LIST_ITEM_PREFIX_PATTERN.match(seg)
        ):
            continue
        safe_segments.append(seg)

    if not safe_segments:
        return None
    combined = " ".join(safe_segments).strip()
    return combined if combined else None


def sanitize_conversation_history(
    history: Sequence[ChatHistoryMessage],
    current_message: str,
) -> tuple[dict[str, str], ...]:
    """Sanitiza histórico antes de encaminhar ao IntentExtractor sem duplicar a mensagem atual."""
    trimmed_history = list(history)
    normalized_current = " ".join(current_message.strip().split()).casefold()
    if trimmed_history:
        last_turn = trimmed_history[-1]
        if last_turn.role == "user" and last_turn.content.casefold() == normalized_current:
            trimmed_history.pop()

    sanitized: list[dict[str, str]] = []
    for turn in trimmed_history:
        if turn.role == "user":
            safe_user = _strip_listing_tokens(turn.content)
            if safe_user:
                sanitized.append({"role": "user", "content": safe_user})
        else:
            safe_assistant = _sanitize_assistant_turn(turn.content)
            if safe_assistant:
                sanitized.append({"role": "assistant", "content": safe_assistant})
    return tuple(sanitized)


def _resolve_applied_budget_brl(
    result: OrchestrationResult,
    explicit_budget_brl: Decimal | None,
) -> Decimal | None:
    if explicit_budget_brl is not None:
        return explicit_budget_brl
    if result.intent is not None and result.intent.price.max_price is not None:
        if result.intent.price.currency in (None, "BRL"):
            return Decimal(str(result.intent.price.max_price))
    return None


def _resolve_metadata(
    result: OrchestrationResult,
    search_response: SearchResponseSchema | None,
) -> tuple[str | None, str | None]:
    if search_response is not None:
        return search_response.detected_category, search_response.profile_id
    detected_category = (
        result.search_plan.canonical_category
        if result.search_plan is not None and result.search_plan.canonical_category is not None
        else (result.intent.category if result.intent is not None else None)
    )
    profile_id = (
        result.ranking_result.active_profile_id
        if result.ranking_result is not None and result.ranking_result.active_profile_id is not None
        else (result.validation_result.active_profile_id if result.validation_result is not None else None)
    )
    return detected_category, profile_id


def _build_success_message(search_response: SearchResponseSchema) -> str:
    parts: list[str] = []
    cat_part = f" na categoria {search_response.detected_category}" if search_response.detected_category else ""
    profile_part = f" usando o perfil {search_response.profile_id}" if search_response.profile_id else ""
    parts.append(
        f"Encontrei {search_response.total_products} produto(s) "
        f"({search_response.total_offers} oferta(s)){cat_part}{profile_part}."
    )

    highlights = search_response.products[:3]
    for group in highlights:
        summary = group.explanation.summary.strip()
        unknown_note = (
            " (aprovado com informações incompletas no anúncio)"
            if group.approval_state == "approved_with_unknowns"
            else ""
        )
        parts.append(f"Destaque #{group.rank}: {group.canonical_title} — {summary}{unknown_note}")

    if search_response.relaxed and search_response.relaxations:
        relaxed_fields = ", ".join(step.field for step in search_response.relaxations)
        parts.append(
            f"Preferências flexíveis relaxadas por escassez de resultados ({relaxed_fields}); "
            "requisitos obrigatórios permaneceram preservados."
        )

    if any(not offer.shipping.known for grp in search_response.products for offer in grp.offers):
        parts.append("Aviso: parte dos anúncios não informa frete, não sendo considerado frete grátis.")
    elif any(offer.validation.unknown_criteria for grp in search_response.products for offer in grp.offers):
        parts.append("Aviso: alguns critérios não informados nos anúncios permanecem marcados como desconhecidos.")

    return " ".join(parts)


def build_chat_response(
    result: OrchestrationResult,
    *,
    request_id: str,
    limit: int,
    explicit_budget_brl: Decimal | None = None,
    converter: CurrencyConverter | None = None,
) -> ChatResponse:
    """Constrói resposta conversacional determinística em PT-BR a partir de OrchestrationResult."""
    applied_budget_brl = _resolve_applied_budget_brl(result, explicit_budget_brl)

    if result.status == ExecutionStatus.CLARIFICATION_NEEDED:
        question = (result.clarification_question or "").strip() or (
            "Pode informar qual tipo de produto você procura e suas principais preferências?"
        )
        detected_category, profile_id = _resolve_metadata(result, None)
        return ChatResponse(
            status="clarification_needed",
            request_id=request_id,
            assistant_message=question,
            clarification_needed=True,
            clarification_question=question,
            search_results=None,
            conversation_context=ChatConversationContextSchema(
                detected_category=detected_category,
                profile_id=profile_id,
                applied_budget_brl=applied_budget_brl,
                clarification_needed=True,
                relaxed=False,
                assistant_turn_summary=question,
            ),
            warnings=[],
        )

    search_response = build_search_response(
        result,
        request_id=request_id,
        limit=limit,
        converter=converter,
    )
    detected_category, profile_id = _resolve_metadata(result, search_response)

    if search_response.status == "ok":
        assistant_message = _build_success_message(search_response)
        cat_label = detected_category or "solicitada"
        prof_label = profile_id or "padrão"
        turn_summary = (
            f"Busca concluída na categoria {cat_label} com perfil {prof_label}. "
            "Você pode ajustar orçamento, marca, perfil ou especificações."
        )
    elif search_response.status == "all_rejected":
        relax_note = ""
        if search_response.relaxed and search_response.relaxations:
            relaxed_fields = ", ".join(step.field for step in search_response.relaxations)
            relax_note = (
                f" Preferências flexíveis ({relaxed_fields}) chegaram a ser relaxadas, "
                "mas ainda assim nenhum candidato atendeu às exigências."
            )
        assistant_message = (
            f"Foram encontrados {search_response.rejected_count} candidato(s), porém todos foram "
            "rejeitados pelos requisitos obrigatórios informados. Os requisitos obrigatórios "
            "não são relaxados automaticamente."
            f"{relax_note} Se desejar, você pode flexibilizar o orçamento ou alguma exigência."
        )
        turn_summary = (
            "Candidatos encontrados foram rejeitados pelos requisitos obrigatórios informados."
        )
    else:
        assistant_message = (
            "Nenhum anúncio foi encontrado na eBay para os critérios informados nesta busca. "
            "Requisitos obrigatórios não foram relaxados. Tente ajustar os termos do produto ou ampliar o escopo."
        )
        turn_summary = "Nenhum anúncio encontrado para os critérios atuais."

    return ChatResponse(
        status=search_response.status,
        request_id=request_id,
        assistant_message=assistant_message,
        clarification_needed=False,
        clarification_question=None,
        search_results=search_response,
        conversation_context=ChatConversationContextSchema(
            detected_category=detected_category,
            profile_id=profile_id,
            applied_budget_brl=applied_budget_brl,
            clarification_needed=False,
            relaxed=search_response.relaxed,
            assistant_turn_summary=turn_summary,
        ),
        warnings=list(search_response.warnings),
    )
