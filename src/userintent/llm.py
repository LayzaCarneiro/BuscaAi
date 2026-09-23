from __future__ import annotations

import os
from typing import Optional

from dotenv import load_dotenv
from google import genai
from google.genai import types

from .prompt import SYSTEM_PROMPT
from .schema import UserIntent

load_dotenv()


class IntentExtractor:
    """Extrai UserIntent usando Gemini Structured Outputs + Pydantic."""

    def __init__(
        self,
        model: Optional[str] = None,
        client: Optional[genai.Client] = None,
    ):
        self.model = model or os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
        self.client = client or genai.Client()

    def extract(self, user_text: str) -> UserIntent:
        """Converte texto livre em UserIntent validado pelo Pydantic."""
        response = self.client.models.generate_content(
            model=self.model,
            contents=user_text,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=UserIntent,
            ),
        )

        # O SDK atual pode expor a resposta estruturada em `parsed`.
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, UserIntent):
            return parsed
        if parsed is not None:
            return UserIntent.model_validate(parsed)

        # Fallback robusto caso a versão do SDK não preencha `response.parsed`.
        text = getattr(response, "text", None)
        if not text:
            raise RuntimeError("Gemini não retornou conteúdo para o UserIntent.")
        return UserIntent.model_validate_json(text)
