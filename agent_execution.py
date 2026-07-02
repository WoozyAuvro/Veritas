import json
from pathlib import Path

from dotenv import load_dotenv

from storage.vector_store import (
    GROQ_CHAT_MODEL,
    GROQ_URL,
    get_groq_api_key,
    post_with_retry,
    search_documents,
)
from tools import FORENSIC_TOOLS, query_sql_ledger, verify_corporate_registry

load_dotenv()


def _normalize_message_content(message_node):
    content = message_node.get("content")
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "".join(parts).strip()
    return content or ""


def _extract_scalar_count(result_text):
    for line in str(result_text).splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return int(candidate)
        except ValueError:
            continue
    raise ValueError(f"Could not parse a numeric count from: {result_text}")


def execute_agent_investigation(flag_cluster_summary, max_turns=5):
    """
    Main orchestrator that hosts the live execution loop for the forensic agent.
    """
    print("Initializing Forensic Investigator Agent for Flag Cluster...")

    api_key = get_groq_api_key()
    if not api_key:
        return "Investigation could not start because GROQ_API_KEY is not configured."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    system_instruction = (
        "You are an elite forensic fraud auditor. Investigate the supplied anomaly cluster using the tools available to you. "
        "Do not guess. Verify each hypothesis against the SQL ledger or the document index before concluding. "
        "If a transaction looks suspicious, query the ledger for context and search the email/receipt corpus for supporting evidence. "
        "Once you have enough proof, provide a definitive report with: Fraud Theory, Evidence Matrix, Severity Rating, and Confidence Score."
    )

    message_history = [
        {"role": "system", "content": system_instruction},
        {"role": "user", "content": f"Investigate these algorithmic flags: {flag_cluster_summary}"},
    ]
    for turn in range(max_turns):
        print(f"[Iteration {turn + 1}/{max_turns}] Consulting Agent Reasoning...")

        body = {
            "model": GROQ_CHAT_MODEL,
            "messages": message_history,
            "tools": FORENSIC_TOOLS,
            "tool_choice": "auto",
            "temperature": 0.05,
        }

        try:
            response = post_with_retry(f"{GROQ_URL}/chat/completions", headers, body, "Groq")
            response_json = response.json()
        except Exception as exc:
            return f"Investigation failed while contacting Groq: {exc}"

        message_node = response_json["choices"][0]["message"]
        message_history.append(message_node)
        with open("history_logs.txt", "+a") as f:
            f.write(str(message_history))
        
        tool_calls = message_node.get("tool_calls")
        if not tool_calls:
            print("Agent reached a definitive conclusion.")
            return _normalize_message_content(message_node)

        for tool_call in tool_calls:
            function_name = tool_call["function"]["name"]
            arguments = json.loads(tool_call["function"]["arguments"])
            call_id = tool_call["id"]

            print(f"Agent called tool: {function_name} with arguments: {arguments}")

            if function_name == "query_sql_ledger":
                tool_result_text = query_sql_ledger(arguments.get("query", ""))
            elif function_name == "search_documents":
                tool_result_text = search_documents(
                    query_text=arguments.get("query_text", ""),
                    n_results=arguments.get("n_results", 2),
                )
            elif function_name == "verify_corporate_registry":
                tool_result_text = verify_corporate_registry(arguments.get("vendor_name", ""))
            else:
                tool_result_text = "Error: Requested tool function does not exist."

            message_history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": function_name,
                    "content": tool_result_text,
                }
            )

    print("Investigation reached max processing depth before natural exit.")
    return _normalize_message_content(message_history[-1]) or "Failed to compile definitive verdict."


if __name__ == "__main__":
    count_result = query_sql_ledger("SELECT COUNT(*) AS total_rows FROM flags;")
    total_rows = _extract_scalar_count(count_result)

    for offset in range(0, total_rows, 5):
        flags = query_sql_ledger(
            f"SELECT reason, date FROM flags ORDER BY date ASC LIMIT 5 OFFSET {offset};"
        )
        final_verdict = execute_agent_investigation(flags)
        print("\n========= FINAL FORENSIC CASE REPORT =========")
        print(final_verdict)