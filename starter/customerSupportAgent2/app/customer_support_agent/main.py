"""
Customer Support AI Agent
==========================
Run locally:
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Gateway auth is read from the environment so no secret lives in this file:
  GATEWAY_TOKEN   a bearer token for the AgentCore Gateway
"""

# ── Imports ───────────────────────────────────────────────────────────────────
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import contextlib
import logging
import uuid
from pathlib import Path
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session


# INFO while debugging so the logger calls below reach CloudWatch.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("CSAI_Agent")


# ── TODO 1 — App Initialisation ───────────────────────────────────────────────

app = BedrockAgentCoreApp()

# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────

GATEWAY_URL = "https://customersupportgateway-y4v3x6giu7.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "F5P6IMV583"
REGION      = "us-east-1"
MEMORY_ID   = "CustomerSupportMemory-21kcW2B3iv"


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────

model_id = "global.amazon.nova-2-lite-v1:0"

model            = BedrockModel(model_id=model_id)
memory_client    = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _first_text(message) -> str | None:
    """Return the message's first text block, or None for tool calls/results.

    Tool results arrive with role "user" but carry a {"toolResult": ...} block
    instead of {"text": ...}. Requiring a text key rejects those and any other
    non-text block in one place, so no caller has to index blindly.
    """
    content = message.get("content") or []
    if not content:
        return None
    block = content[0]
    if "text" not in block:
        return None
    return block["text"]


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    CONTEXT_HEADER = "Customer Context:"

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id

        # Resolved once per request: a network call whose result cannot change
        # mid-conversation.
        self.namespaces = get_namespaces(memory_client, memory_id)

        # The customer's message before context injection. Saving the injected
        # version would feed retrieved memories back into memory every turn.
        self._original_query = None

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        messages = event.agent.messages
        if not messages:
            return

        last_msg = messages[-1]
        if last_msg.get("role") != "user":
            return

        user_query = _first_text(last_msg)
        if not user_query or not user_query.strip():
            return

        self._original_query = user_query

        blocks = []
        for strategy_type, ns_template in self.namespaces.items():
            namespace = ns_template.format(actorId=self.actor_id)
            try:
                memories = self.memory_client.retrieve_memories(
                    memory_id=self.memory_id,
                    namespace=namespace,
                    query=user_query,
                    top_k=5,
                )
            except Exception as e:
                # Retrieval is best-effort: a memory failure must not break the
                # customer's request.
                logger.warning("Memory retrieval failed for %s: %s", namespace, e)
                continue

            texts = []
            for memory in memories:
                memory_content = memory.get("content")
                if isinstance(memory_content, dict):
                    memory_txt = memory_content.get("text")
                    if memory_txt and memory_txt.strip():
                        texts.append(f"- {memory_txt.strip()}")

            if texts:
                # Tag each group so the model can tell a preference the customer
                # stated from a fact the service inferred.
                label = strategy_type.replace("_", " ").title()
                blocks.append(f"[{label}]\n" + "\n".join(texts))

        # Injection happens once, after every namespace has been queried.
        if not blocks:
            logger.info("No memories found for actor %s", self.actor_id)
            return

        context = "\n\n".join(blocks)
        last_msg["content"][0]["text"] = (
            f"{self.CONTEXT_HEADER}\n{context}\n\n{user_query}"
        )
        logger.info(
            "Injected %d memory group(s) for actor %s", len(blocks), self.actor_id
        )

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        messages = event.agent.messages
        if not messages:
            return

        agent_response = None
        customer_query = None

        # Walk backwards: the assistant's answer is near the end, and the
        # customer's question sits behind however many tool round trips the
        # agent needed.
        for message in reversed(messages):
            text = _first_text(message)
            if text is None:
                continue  # skip tool calls and tool results

            role = message.get("role")
            if role == "assistant" and agent_response is None:
                agent_response = text
            elif role == "user" and customer_query is None:
                customer_query = text

            if agent_response is not None and customer_query is not None:
                break

        if agent_response is None or customer_query is None:
            logger.warning("Incomplete turn; nothing saved to memory")
            return

        # Prefer the pre-injection text captured on the read path.
        if self._original_query is not None:
            customer_query = self._original_query

        try:
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                # (text, role) tuples — content first, role second.
                messages=[
                    (customer_query, "USER"),
                    (agent_response, "ASSISTANT"),
                ],
            )
            logger.info(
                "Saved turn for actor %s session %s", self.actor_id, self.session_id
            )
        except Exception as e:
            # A memory write failure must not turn a good answer into a 500.
            logger.error("Failed to save interaction: %s", e)
        finally:
            self._original_query = None

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────

@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    # The unconfigured value is the placeholder "<kbid>", which is truthy, so an
    # emptiness check alone never fires.
    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured."

    # retrievalConfiguration takes one of two shapes depending on how the
    # knowledge base was created: managedSearchConfiguration for a managed KB,
    # vectorSearchConfiguration for one backed by your own vector store. Sending
    # the wrong one is a ValidationException, so try the managed shape first and
    # fall back rather than hard-coding either.
    for search_config in ("managedSearchConfiguration", "vectorSearchConfiguration"):
        try:
            resp = _bedrock_runtime.retrieve(
                knowledgeBaseId=KB_ID,
                retrievalQuery={"text": query},
                retrievalConfiguration={search_config: {"numberOfResults": 5}},
            )
            break
        except _bedrock_runtime.exceptions.ValidationException as e:
            if "not supported" in str(e) and search_config == "managedSearchConfiguration":
                logger.info("Managed search rejected; retrying with vector search")
                continue
            logger.error("Knowledge base retrieval failed: %s", e)
            return f"Could not search the knowledge base right now: {e}"
        except Exception as e:
            logger.error("Knowledge base retrieval failed: %s", e)
            return f"Could not search the knowledge base right now: {e}"
    else:
        return "Could not search the knowledge base right now."

    results = resp.get("retrievalResults", [])

    chunks = []
    for result in results:
        content = result.get("content") or {}
        text = content.get("text", "")
        if text and text.strip():
            chunks.append(text.strip())

    if not chunks:
        return f"No information found in the knowledge base for: {query}"

    # Never return None — the model renders it as a failed tool call.
    return "\n---\n".join(chunks)


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────

# Plain (non-f) string on purpose: the body is full of dict literals, and inside
# an f-string every brace would have to be doubled. Only the four arguments are
# interpolated, in the tool below.
_DISCOUNT_LOGIC = '''
import json

earn_rates = {"standard": 1, "device": 2, "fresh": 5}
tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}

# Accept "gold", "GOLD", " Gold " etc.
tier_key = str(tier).strip().title()
tier_rate = tier_rates.get(tier_key, 0.00)

category_key = str(product_category).strip().lower()
earn_rate = earn_rates.get(category_key, 1)

# Points redeem in blocks of 500, at 100 points = $1.
usable_points = (int(loyalty_points) // 500) * 500

# Redemption may not cover more than half the order. Recomputed as whole blocks
# so the cap never produces a fractional 500-point block.
max_redeem_value = order_total * 0.5
max_points = (int(max_redeem_value * 100) // 500) * 500
points_redeemed = min(usable_points, max_points)
points_value = round(points_redeemed / 100, 2)

# Tier discount applies to what is left after points are spent.
subtotal = round(order_total - points_value, 2)
tier_discount = round(subtotal * tier_rate, 2)
final_total = round(subtotal - tier_discount, 2)
total_savings = round(points_value + tier_discount, 2)

# Design decision: points are earned on what the customer actually pays, not on
# the pre-discount order total.
points_earned = int(final_total * earn_rate)
remaining_points = int(loyalty_points) - points_redeemed + points_earned

result = {
    "tier": tier_key,
    "product_category": category_key,
    "order_total": round(order_total, 2),
    "points_balance": int(loyalty_points),
    "points_redeemed": points_redeemed,
    "points_value_usd": points_value,
    "subtotal_after_points": subtotal,
    "tier_discount_rate": tier_rate,
    "tier_discount_usd": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
}
print(json.dumps(result))
'''


@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        Full discount breakdown and final price
    """
    code = (
        f"loyalty_points = {int(loyalty_points)}\n"
        f"tier = {str(tier)!r}\n"
        f"order_total = {float(order_total)}\n"
        f"product_category = {str(product_category)!r}\n"
        + _DISCOUNT_LOGIC
    )

    try:
        with code_session(REGION) as session:
            response = session.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    # Each call is independent — no state leaks between customers.
                    "clearContext": True,
                },
            )

            for event in response["stream"]:
                if "result" not in event:
                    continue

                result = event["result"]

                # Prefer the printed JSON: it hands the model clean fields
                # rather than protocol wrapping.
                for item in result.get("content", []):
                    if item.get("type") == "text":
                        text = (item.get("text") or "").strip()
                        if not text:
                            continue
                        try:
                            return json.dumps(json.loads(text))
                        except json.JSONDecodeError:
                            logger.warning(
                                "Code Interpreter output was not JSON: %s", text
                            )

                return json.dumps(result)

            raise RuntimeError("Code Interpreter returned no result event")

    except Exception as e:
        # The sandbox can be unavailable or throttled. A tool that raises kills
        # the whole turn, so degrade instead and flag it clearly.
        logger.error("Code Interpreter unavailable, using fallback: %s", e)

        tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
        tier_key = str(tier).strip().title()
        tier_rate = tier_rates.get(tier_key, 0.00)

        tier_discount = round(order_total * tier_rate, 2)
        final_total = round(order_total - tier_discount, 2)

        return json.dumps({
            "tier": tier_key,
            "order_total": round(order_total, 2),
            "points_redeemed": 0,
            "tier_discount_rate": tier_rate,
            "tier_discount_usd": tier_discount,
            "final_total": final_total,
            "total_savings": tier_discount,
            "degraded": True,
            "note": (
                "Calculated without the code sandbox: tier discount only, "
                "loyalty points were not applied."
            ),
        })


# ── TODO 8 — Agent Entrypoint ─────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a customer support agent for an Amazon store.
You are warm, concise, and never invent information.

Choosing a tool:
- Order status, tracking, delivery dates, a customer's order history or
  profile -> the order tracking tools from the gateway.
- Refunds, refund status, return shipping labels -> the refund tools from
  the gateway.
- Product specifications, return and warranty policies, loyalty program
  rules, order status definitions -> search_knowledge_base.
- Any arithmetic on points, discounts or totals -> calculate_loyalty_discount.
  Never compute a discount yourself.
- Live information from a public website -> the browser tool.

Rules:
- Look up the customer's tier and points before calculating a discount;
  do not ask them to supply values a tool can retrieve.
- If a tool fails, say plainly what you could not retrieve. Never guess an
  order status, refund amount or price.
- If a discount result is marked degraded, say the points balance was not
  applied and the total shown is provisional.
- Any text under "Customer Context:" is what you remember about this
  customer. Use it naturally; never quote it back or mention memory."""


_DRIVER_READY = False


def _ensure_playwright_driver() -> None:
    """Make playwright's bundled Node driver executable.

    A zip archive records POSIX permissions, but a package built on Windows has
    no executable bit to record, so CodeZip ships driver/node without one and
    playwright fails with PermissionError when it tries to spawn it.

    Fixed in place where the deployment directory is writable; otherwise the
    binary is copied to /tmp and playwright is pointed at the copy through
    PLAYWRIGHT_NODEJS_PATH, which compute_driver_executable() honours.
    """
    global _DRIVER_READY
    if _DRIVER_READY:
        return

    import inspect, shutil, stat
    import playwright

    driver = Path(inspect.getfile(playwright)).parent / "driver" / "node"
    if not driver.exists():
        logger.warning("Playwright driver not found at %s", driver)
        _DRIVER_READY = True
        return

    if os.access(driver, os.X_OK):
        _DRIVER_READY = True
        return

    try:
        driver.chmod(driver.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        logger.info("Marked playwright driver executable in place")
    except OSError:
        # Deployment directory is read-only — use a writable copy instead.
        target = Path("/tmp/playwright-driver/node")
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(driver, target)
        target.chmod(0o755)
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(target)
        logger.info("Copied playwright driver to %s", target)

    _DRIVER_READY = True


def _gateway_headers() -> Dict:
    """Auth headers for the Gateway, or empty if no token is configured."""
    token = os.environ.get("GATEWAY_TOKEN")
    if not token:
        logger.warning("GATEWAY_TOKEN is not set; gateway tools will be unavailable")
        return {}
    return {"Authorization": f"Bearer {token}"}


@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    # Imported here rather than at module scope: it pulls in playwright, which
    # is slow, and only requests that browse should pay for it.
    from strands_tools.browser import AgentCoreBrowser

    _ensure_playwright_driver()

    user_input = (payload or {}).get("prompt")
    if not user_input or not str(user_input).strip():
        return "I did not receive a question. What can I help you with?"

    # actor_id is what AgentCore Memory calls the payload's customer_id. The
    # fallback keeps the namespace valid instead of "cs_agent/None/facts".
    actor_id = payload.get("customer_id") or "anonymous"
    session_id = payload.get("session_id") or str(uuid.uuid4())

    try:
        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
        agent_browser = AgentCoreBrowser(region=REGION)

        local_tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_browser.browser,
        ]

        mcp_client = MCPClient(url=GATEWAY_URL, headers=_gateway_headers())

        # list_tools_sync() and every gateway tool call need a live MCP session,
        # so the agent must be invoked inside this block. Loading the tools here
        # and invoking outside it looks fine at startup and then fails on the
        # first order lookup with MCPClientInitializationError.
        with contextlib.ExitStack() as stack:
            gateway_tools = []
            try:
                stack.enter_context(mcp_client)
                gateway_tools = list(mcp_client.list_tools_sync())
                logger.info("Loaded %d gateway tool(s)", len(gateway_tools))
            except Exception as e:
                # Degrade rather than fail: knowledge base, discounts and the
                # browser still work without the gateway.
                logger.error("Gateway unavailable, continuing without it: %s", e)

            agent = Agent(
                model=model,
                tools=local_tools + gateway_tools,
                system_prompt=SYSTEM_PROMPT,
                hooks=[memory_hook],
            )

            result = await agent.invoke_async(user_input)

        for block in result.message.get("content", []):
            if "text" in block:
                return block["text"]

        return "I could not produce a response. Please try rephrasing."

    except Exception as e:
        logger.exception("Request failed for actor %s session %s", actor_id, session_id)
        return f"Sorry, something went wrong handling that request: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    # app.run() starts the HTTP server AgentCore Runtime connects to. It must
    # be the live line whenever this file is deployed.
    app.run()
    # For local CLI testing only, comment out app.run() and uncomment main().
    # Never deploy with main() live: argparse exits immediately on a container
    # start, the server never binds, and AgentCore reports it as
    # "Runtime initialization time exceeded".
    # main()