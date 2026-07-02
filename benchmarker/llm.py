"""Couche d'accès au LLM Claude.

Regroupe la construction du client, la boucle d'outils serveur (recherche web /
web fetch, avec gestion du `pause_turn`) et l'extraction structurée typée.
"""

from __future__ import annotations

from typing import Any, List, Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel

from . import config

T = TypeVar("T", bound=BaseModel)


def build_client() -> anthropic.Anthropic:
    """Construit le client Anthropic.

    Les identifiants sont résolus depuis l'environnement (ANTHROPIC_API_KEY,
    ANTHROPIC_AUTH_TOKEN, ou un profil `ant auth login`).
    """
    return anthropic.Anthropic()


def _collect_text(content: list) -> str:
    """Concatène les blocs texte d'une réponse."""
    chunks: List[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            chunks.append(block.text)
    return "\n".join(chunks).strip()


def run_server_tool_loop(
    client: anthropic.Anthropic,
    *,
    system: str,
    user: str | list,
    tools: list,
    effort: str = "high",
    max_tokens: int = 16000,
) -> str:
    """Exécute une conversation utilisant des outils serveur (recherche/fetch).

    Gère le `stop_reason == "pause_turn"` en renvoyant l'assistant pour reprise,
    avec un garde-fou sur le nombre de continuations. Renvoie le texte agrégé
    de la réponse finale.
    """
    messages: list = [{"role": "user", "content": user}]

    response = client.messages.create(
        model=config.MODEL,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        tools=tools,
        messages=messages,
    )

    continuations = 0
    while response.stop_reason == "pause_turn" and continuations < config.MAX_CONTINUATIONS:
        continuations += 1
        messages.append({"role": "assistant", "content": response.content})
        response = client.messages.create(
            model=config.MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            tools=tools,
            messages=messages,
        )

    if response.stop_reason == "refusal":
        raise RuntimeError("Le modèle a refusé la requête (stop_reason=refusal).")

    return _collect_text(response.content)


def parse_structured(
    client: anthropic.Anthropic,
    *,
    schema: Type[T],
    instruction: str,
    material: str,
    effort: str = "medium",
    max_tokens: int = 16000,
) -> Optional[T]:
    """Structure du texte libre en un modèle Pydantic via `messages.parse`.

    `material` est le texte brut (résultats de recherche / contenu de page) à
    structurer ; `instruction` explique ce qu'on attend.
    """
    prompt = f"{instruction}\n\n--- MATÉRIAU À STRUCTURER ---\n{material}"
    response = client.messages.parse(
        model=config.MODEL,
        max_tokens=max_tokens,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    return response.parsed_output
