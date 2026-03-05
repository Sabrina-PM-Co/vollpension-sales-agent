#!/usr/bin/env python3
"""
Agent Runner – Gemeinsamer Agentic Tool Loop
============================================
Wird von allen drei Workflows genutzt:
  - offer_workflow.py     (Angebote)
  - invoice_workflow.py   (Rechnungen AR/SR)
  - dunning_workflow.py   (Mahnungen)

Der Caller übergibt eigene `tools` + `tool_map` – keine globalen
Tool-Definitionen hier, da jeder Workflow unterschiedliche Tools nutzt.
"""

import json
import anthropic
from config import ANTHROPIC_API_KEY
from audit_logger import start_run, log_turn, finish_run

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ─── Gemeinsamer Agentic Loop ─────────────────────────────────────────────────

def run_agent(
    system_prompt: str,
    initial_message: str,
    tools: list,
    tool_map: dict,
    workflow_type: str = "unknown",
    deal_id: int | None = None,
    max_turns: int = 15,
) -> dict:
    """
    Führt den Claude-Agenten mit manuellem Tool-Loop aus.
    Loggt jeden Turn via audit_logger (nur Metadaten, kein PII).

    Args:
        system_prompt:    Workflow-spezifischer System-Prompt.
        initial_message:  Erste User-Nachricht (Aufgabe).
        tools:            Claude-Tool-Definitionen (Liste von Dicts).
        tool_map:         Callable-Map {tool_name: lambda input_dict → str}.
        workflow_type:    Logging-Kategorie (z.B. 'offer', 'ar_invoice', 'dunning').
        deal_id:          Pipedrive-Deal-ID (optional, für Logging).
        max_turns:        Maximale Anzahl Tool-Runden vor Abbruch.

    Returns:
        {
          "summary":      str,   # Letzter Text-Output des Agenten
          "tool_results": dict,  # Gesammelte Extraktionen aus Tool-Ergebnissen
          "error":        str | None,
          "run_id":       str,
        }
    """
    model = "claude-opus-4-5-20251101"
    run_id = start_run(workflow_type=workflow_type, deal_id=deal_id, model=model)

    messages = [{"role": "user", "content": initial_message}]
    collected: dict = {}

    try:
        for turn in range(max_turns):
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt,
                tools=tools,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            tool_names = [b.name for b in tool_use_blocks]

            log_turn(
                run_id=run_id,
                turn_number=turn + 1,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                tool_names=tool_names,
                stop_reason=response.stop_reason,
            )

            if response.stop_reason == "end_turn":
                summary = ""
                for block in response.content:
                    if hasattr(block, "text"):
                        summary = block.text
                finish_run(run_id, status="completed")
                return {
                    "summary": summary,
                    "tool_results": collected,
                    "error": None,
                    "run_id": run_id,
                }

            if not tool_use_blocks:
                break

            tool_results = []
            for tb in tool_use_blocks:
                executor = tool_map.get(tb.name)
                try:
                    output = executor(tb.input) if executor else f"Unbekanntes Tool: {tb.name}"
                except Exception as e:
                    output = f"Tool-Fehler ({tb.name}): {e}"

                _collect(tb.name, tb.input, output, collected)

                tool_results.append({
                    "type":        "tool_result",
                    "tool_use_id": tb.id,
                    "content":     str(output),
                })

            messages.append({"role": "user", "content": tool_results})

        finish_run(run_id, status="max_turns")
        return {
            "summary": "Max Turns erreicht",
            "tool_results": collected,
            "error": "max_turns",
            "run_id": run_id,
        }

    except Exception as e:
        finish_run(run_id, status="failed", error=str(e))
        return {
            "summary": "",
            "tool_results": collected,
            "error": str(e),
            "run_id": run_id,
        }


# ─── Tool-Ergebnis-Extraktion ─────────────────────────────────────────────────

def _collect(tool_name: str, tool_input: dict, output: str, collected: dict):
    """
    Extrahiert relevante Werte aus Tool-Ergebnissen und speichert sie in `collected`.
    Wird nach jedem Tool-Call aufgerufen.
    """
    try:
        data = json.loads(output) if isinstance(output, str) else output
    except Exception:
        return

    if tool_name == "pipedrive_get_deal":
        collected["deal_title"]    = data.get("title", "")
        collected["deal_value"]    = float(data.get("value", 0) or 0)
        collected["event_datum"]   = data.get("event_datum", "")
        collected["contact_id_pd"] = data.get("person_id", "")
        collected["org_id"]        = data.get("org_id", "")

    elif tool_name in ("sevdesk_search_contact", "sevdesk_create_contact"):
        collected["contact_id_sdsk"] = data.get("contact_id", "")
        collected["contact_email"]   = data.get("email", "")
        collected["contact_name"]    = data.get("name", "")

    elif tool_name == "sevdesk_create_offer_draft":
        collected["offer_id"]     = data.get("order_id", "")
        collected["offer_number"] = data.get("order_number", "")
        collected["offer_link"]   = data.get("sevdesk_link", "")
        collected["valid_until"]  = data.get("valid_until", "")

    elif tool_name == "sevdesk_delete_offer":
        if data.get("status") == "deleted":
            collected["offer_deleted"]        = True
            collected["deleted_order_id"]     = data.get("order_id", "")
            collected["deleted_order_number"] = data.get("order_number", "")

    elif tool_name == "sevdesk_create_invoice":
        inv_type = tool_input.get("invoice_type", "RE")
        if inv_type == "AR":
            collected["ar_invoice_id"]     = data.get("invoice_id", "")
            collected["ar_invoice_number"] = data.get("invoice_number", "")
            collected["ar_amount"]         = float(
                (tool_input.get("positions") or [{}])[0].get("price", 0)
            )
            collected["ar_sevdesk_link"]   = data.get("sevdesk_link", "")
            collected["ar_invoice_date"]   = data.get("invoice_date", "")
        else:
            collected["sr_invoice_id"]     = data.get("invoice_id", "")
            collected["sr_invoice_number"] = data.get("invoice_number", "")
            collected["sr_sevdesk_link"]   = data.get("sevdesk_link", "")

    elif tool_name == "sevdesk_cancel_invoice":
        collected["storno_id"]     = data.get("cancel_invoice_id", "")
        collected["storno_number"] = data.get("cancel_invoice_number", "")

    elif tool_name == "sevdesk_create_dunning":
        collected["dunning_id"]     = data.get("dunning_id", "")
        collected["dunning_number"] = data.get("dunning_number", "")
        collected["dunning_link"]   = data.get("sevdesk_link", "")
