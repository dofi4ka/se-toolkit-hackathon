"""System prompts for multi-mode assistant."""

from __future__ import annotations

from typing import Any


def system_choosing(*, query: str, candidates: list[dict[str, Any]]) -> str:
    lines = [
        "You help the user choose ONE recipe from a fixed list shown in the Telegram bot.",
        "Format replies in Markdown (bold, lists, code) where helpful.",
        "Be concise. Compare options when asked. Do not invent URLs or recipes outside the list.",
        "The user taps numbered buttons to select; your job is discussion only.",
        f"\nSearch query: {query}\n",
        "Recipe options:",
    ]
    for i, c in enumerate(candidates[:10]):
        title = (c.get("title") or "").strip() or "Untitled"
        url = (c.get("url") or "").strip()
        lines.append(f"  {i + 1}. {title}\n     {url}")
    return "\n".join(lines)


def _recipe_block(recipe: dict[str, Any]) -> str:
    title = (recipe.get("title") or "Recipe").strip()
    ings = recipe.get("ingredients") or []
    steps = recipe.get("steps") or []
    ing_lines = "\n".join(f"  - {x}" for x in ings)
    step_lines = "\n".join(f"  {j + 1}. {s}" for j, s in enumerate(steps))
    return (
        f"Title: {title}\n\n"
        f"Ingredients:\n{ing_lines}\n\n"
        f"Steps:\n{step_lines}\n"
    )


def system_checklist(*, recipe: dict[str, Any]) -> str:
    return (
        "You help the user gather ingredients for the recipe below: substitutions, where to buy, "
        "metric/imperial, dietary swaps, and checking what they already have. "
        "Format replies in Markdown (bold, lists, code) where helpful. "
        "Stay grounded in this recipe. Be concise.\n\n"
        + _recipe_block(recipe)
    )


def system_rewrite_step() -> str:
    return (
        "You rewrite a single recipe cooking step for clarity and readability. "
        "Preserve meaning, times, temperatures, amounts, and ingredient names. "
        "Do not add new steps, omit safety notes, or invent ingredients. "
        "Reply with ONLY the rewritten step text — no title, no preamble, no markdown code fences."
    )


def user_rewrite_step(*, recipe: dict[str, Any], step_index: int) -> str:
    steps = recipe.get("steps") or []
    cur = ""
    if 0 <= step_index < len(steps):
        cur = str(steps[step_index]).strip()
    block = _recipe_block(recipe)
    return (
        f"Full recipe context:\n{block}\n"
        f"Rewrite step {step_index + 1} only.\n\nOriginal step text:\n{cur}"
    )


def system_cooking(*, recipe: dict[str, Any], step_index: int) -> str:
    steps = recipe.get("steps") or []
    cur = ""
    if 0 <= step_index < len(steps):
        cur = steps[step_index]
    block = _recipe_block(recipe)
    return (
        "You help the user cook the recipe below: technique, timing, temperature, troubleshooting. "
        "Format replies in Markdown (bold, lists, code) where helpful. "
        "Stay grounded in this recipe. Be concise.\n\n"
        f"{block}\n"
        f"Current step number (1-based): {step_index + 1}\n"
        f"Current step text: {cur}\n"
    )
