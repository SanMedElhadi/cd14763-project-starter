"""
Customer Support AI Agent — Starter Code
==========================================
Your task is to complete this file by implementing all sections marked
with # TODO comments.

Reference the step-by-step solution files and INSTRUCTIONS.md for guidance.
Do NOT copy the solution directly — work through each section yourself.

Run locally (after filling in config values):
  uv run main.py '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'

Deploy to AgentCore:
  agentcore deploy

Invoke deployed agent:
  agentcore invoke '{"prompt": "Hello", "customer_id": "CUST-123", "session_id": "s1"}'
"""

# ── Imports ───────────────────────────────────────────────────────────────────
# These imports are provided. Do not remove them.
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
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")

# ── TODO 1 — App Initialisation ───────────────────────────────────────────────
# Create a BedrockAgentCoreApp instance.
# This registers the ASGI server for AgentCore deployment.
# There must be exactly one instance per deployment.
#
# Hint: app = BedrockAgentCoreApp()

# TODO: Create the BedrockAgentCoreApp instance
app = BedrockAgentCoreApp()  # Replace this line


# Suppress interactive tool-consent prompts (required in headless deployments).
os.environ["BYPASS_TOOL_CONSENT"] = "true"


# ── TODO 2 — Configuration ────────────────────────────────────────────────────
# Replace the placeholder strings with your actual AWS resource values.
# You collected these in Part 1 of the INSTRUCTIONS.
#
# GATEWAY_URL format: https://<alias>.gateway.bedrock-agentcore.<region>.amazonaws.com/mcp
# KB_ID       format: 10-character alphanumeric string from the KB console
# REGION:     your AWS region, e.g. "us-east-1"
# MEMORY_ID   format: shown in the AgentCore Memory console

GATEWAY_URL = "https://customersupportgateway-y4v3x6giu7.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"   # TODO: Replace with your Gateway URL
KB_ID       = "F5P6IMV583"          # TODO: Replace with your Knowledge Base ID
REGION      = "us-east-1"        # TODO: Replace with your AWS region
MEMORY_ID   = "CustomerSupportMemory-21kcW2B3iv"        # TODO: Replace with your Memory ID


# ── TODO 3 — Model and Clients ────────────────────────────────────────────────
# Create:
#   1. A BedrockModel using model_id "global.amazon.nova-2-lite-v1:0"
#   2. A MemoryClient with region_name=REGION
#   3. A boto3 client for the "bedrock-agent-runtime" service in REGION
#
# Hint: model = BedrockModel(model_id=model_id)

model_id = "global.amazon.nova-2-lite-v1:0"

# TODO: Create the BedrockModel instance
model = BedrockModel(model_id=model_id)  # Replace this line

# TODO: Create the MemoryClient instance
memory_client = MemoryClient(region_name=REGION)  # Replace this line

# TODO: Create the boto3 bedrock-agent-runtime client
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)  # Replace this line


# ── TODO 4 — Namespace Helper ─────────────────────────────────────────────────
# Implement get_namespaces() to return a dict mapping strategy type to
# namespace template string.
#
# Steps:
#   1. Call mem_client.get_memory_strategies(memory_id) to get strategy list
#   2. Return a dict: { strategy["type"]: strategy["namespaces"][0] for each strategy }
#
# Example output:
#   { "SEMANTIC": "cs_agent/{actorId}/facts",
#     "USER_PREFERENCE": "cs_agent/{actorId}/preferences" }

def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    # TODO: Implement this function
    strategies = mem_client.get_memory_strategies(memory_id)
    return {s["type"]: s["namespaces"][0] for s in strategies}


# ── TODO 5 — Memory Hook ──────────────────────────────────────────────────────
# Implement MemoryHook, a HookProvider subclass that adds long-term memory.
#
# The class needs:
#   __init__(self, actor_id, session_id, memory_client, memory_id)
#     — store all four as instance attributes
#     — call get_namespaces() and store the result as self.namespaces
#
#   retrieve_customer_context(self, event: MessageAddedEvent)
#     — only runs for plain-text user messages (not tool results)
#     — for each strategy namespace, call memory_client.retrieve_memories(
#          memory_id, namespace (formatted with actorId), query, top_k=5)
#     — collect non-empty memory texts tagged with their strategy type
#     — if any memories found, prepend them to the user message as:
#          "Customer Context:\n<memories>\n\n<original_message>"
#
#   save_support_interaction(self, event: AfterInvocationEvent)
#     — walk the message list backwards to find the last plain-text user
#       query and the last assistant response
#     — call memory_client.create_event(memory_id, actor_id, session_id,
#          messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")])
#
#   register_hooks(self, registry: HookRegistry)
#     — register retrieve_customer_context on MessageAddedEvent
#     — register save_support_interaction on AfterInvocationEvent

class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        # TODO: Store actor_id, session_id, memory_id, memory_client as attributes
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_client = memory_client
        self.memory_id = memory_id
        # TODO: Call get_namespaces() and store the result as self.namespaces
        self.namespaces = get_namespaces(self.memory_client, self.memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        # TODO: Implement memory retrieval
        # Steps:
        #   1. Get the last message from event.agent.messages
        #   2. Check it is a user message and not a tool result
        #   3. Extract the user query text
        #   4. For each namespace in self.namespaces, call retrieve_memories()
        #   5. Collect non-empty memory texts with strategy type tags
        #   6. If any found, prepend them to the user message
        last_msg = event.agent.messages[-1]
        if(last_msg.get("role") == "user"):
            user_query = last_msg.get("content")[0]["text"]
            blocks = []
            for strategy_type, ns_template in self.namespaces.items() :
                try:
                    memories = self.memory_client.retrieve_memories(
                        memory_id= self.memory_id,
                        namespace=ns_template.format(actorId=self.actor_id),
                        query=user_query,
                        top_k=5)
                except Exception as e:
                    # Retrieval is best-effort: a memory failure must not break the
                    # customer's request.
                    logger.warning("Memory retrieval failed for %s: %s", ns_template.format(actorId=self.actor_id), e)
                    continue
                texts = []
                for memory in memories:
                    memory_content = memory.get("content")
                    if memory_content and isinstance(memory_content, dict):
                        memory_txt = memory_content.get("text")
                        if memory_txt:
                            texts.append(f"- {memory_txt.strip()}")
                if texts:
                    label = strategy_type.replace("_", " ").title()
                    blocks.append(f"[{label}]\n" + "\n".join(texts))

                    if not blocks:
                        logger.info("No memories found for actor %s", self.actor_id)
                        return
                    context = "\n\n".join(blocks)
                    last_msg.get("content")[0]["text"] = f"{self.CONTEXT_HEADER}\n{context}\n\n{user_query}"
                    logger.info("Injected %d memory group(s) for actor %s", len(blocks), self.actor_id)
        return

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        # TODO: Implement memory saving
        # Steps:
        #   1. Get messages from event.agent.messages
        #   2. Walk backwards to find the last user query (plain text)
        #      and the last assistant response
        #   3. Call memory_client.create_event() with both messages
        messages = event.agent.messages
        if not messages:
            return

        agent_response = None
        customer_query = None

        for message in reversed(messages):
            content = message.get("content")
            if not content:
                return
            text = content[0]["text"]
            role = message.get("role")

            if role == "assistant" and agent_response is None:
                agent_response = text
            elif role == "user" and customer_query is None:
                customer_query = text

            if agent_response is not None and customer_query is not None:
                break

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

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        # TODO: Register retrieve_customer_context on MessageAddedEvent
        # TODO: Register save_support_interaction on AfterInvocationEvent
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 — Knowledge Base Tool ─────────────────────────────────────────────
# Implement search_knowledge_base(query) using the @tool decorator.
#
# Steps:
#   1. Guard: if KB_ID is empty return "Knowledge base not configured."
#   2. Call _bedrock_runtime.retrieve(
#          knowledgeBaseId=KB_ID,
#          retrievalQuery={"text": query}
#      )
#   3. Extract resp["retrievalResults"]; return a message if empty
#   4. Join the text chunks with "\n---\n" and return the result
#
# The docstring is the tool description — the model uses it to decide when
# to call this tool, so keep it clear and accurate.

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
    # TODO: Implement the Knowledge Base search

    if not KB_ID or KB_ID.startswith("<"):
        return "Knowledge base not configured."
    try:
        resp    = _bedrock_runtime.retrieve(knowledgeBaseId=KB_ID,
                                        retrievalQuery={"text": query})
    except Exception as e:
        logger.error("Knowledge base retrieval failed: %s", e)
        return f"Could not search the knowledge base right now: {e}"

    results = resp.get("retrievalResults", [])

    if not results:
        return f"No information found in the knowledge base for: {query}"

    chunks = []
    for result in results:
        content = result.get("content") or {}
        text = content.get("text", "")
        if text and text.strip():
            chunks.append(text.strip())
    if not chunks:
        return "\n---\n".join(chunks)
    else :
        return f"No information found in the knowledge base for: {query}"


# ── TODO 7 — Loyalty Discount Tool (Code Interpreter) ────────────────────────
# Implement calculate_loyalty_discount() using the @tool decorator.
#
# The tool must:
#   1. Build a self-contained Python code string that:
#        • Defines earn_rates: {"standard": 1, "device": 2, "fresh": 5}
#        • Defines tier_rates: {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
#        • Calculates points_redeemed (floor to nearest 500, cap at 50% of order)
#        • Calculates tier_discount (applied to subtotal after points)
#        • Calculates final_total, total_savings, points_earned, remaining_points
#        • Prints a JSON result dict
#   2. Execute the code with code_session(REGION).invoke("executeCode", {...})
#      using language="python" and clearContext=True
#   3. Return the first result event as a JSON string
#   4. Include a fallback that computes only the tier discount if the
#      Code Interpreter is unavailable

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
    # TODO: Build the code string (use an f-string to inject the arguments)
    code = f"loyalty_points = {int(loyalty_points)}\n" + f"tier = {str(tier)!r}\n" + f"order_total = {float(order_total)}\n" + f"product_category = {str(product_category)!r}\n" +'''
import json, math
 
earn_rates = {"standard": 1, "device": 2, "fresh": 5}
tier_rates = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
 
# Accept "gold", "GOLD", " Gold " etc.
tier_key = str(tier).strip().title()
tier_rate = tier_rates.get(tier_key, 0.00)
 
category_key = str(product_category).strip().lower()
earn_rate = earn_rates.get(category_key, 1)
 
# Points redeem in blocks of 500, at 100 points = $1.
usable_points = (int(loyalty_points) // 500) * 500
 
# Redemption may not cover more than half the order. Recomputed as whole
# blocks so the cap never produces a fractional 500-point block.
max_redeem_value = order_total * 0.5
max_points = (int(max_redeem_value * 100) // 500) * 500
points_redeemed = min(usable_points, max_points)
points_value = round(points_redeemed / 100, 2)
 
# Tier discount applies to what is left after points are spent.
subtotal = round(order_total - points_value, 2)
tier_discount = round(subtotal * tier_rate, 2)
final_total = round(subtotal - tier_discount, 2)
total_savings = round(points_value + tier_discount, 2)
 
# Design decision: points are earned on what the customer actually pays,
# not on the pre-discount order total.
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

    try:
        # TODO: Execute the code using code_session and return the result
        with code_session(REGION) as session:
            response = session.invoke(
                "executeCode",
                {
                    "code": code,
                    "language": "python",
                    "clearContext": True,
                },
            )

            for event in response["stream"]:
                if "result" not in event:
                    continue
 
                result = event["result"]
                for item in result.get("content", []):
                    if item.get("type") == "text":
                        text = (item.get("text") or "").strip()
                        if not text:
                            continue
                        try:
                            return json.dumps(json.loads(text))
                        except json.JSONDecodeError:
                            # Sandbox raised — fall through to the fallback.
                            logger.warning("Code Interpreter output was not JSON: %s", text)
 
                return json.dumps(result)

    except Exception as e:

        logger.error("Code Interpreter unavailable, using fallback: %s", e)
        # TODO: Implement fallback calculation using tier discount only
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
# Implement the invoke() function decorated with @app.entrypoint.
#
# Steps:
#   1. Extract user_input, actor_id, and session_id from the payload
#      (generate a UUID if session_id is missing)
#   2. Instantiate MemoryHook for this actor/session
#   3. Instantiate AgentCoreBrowser(region=REGION)
#   4. Build the tools list: [search_knowledge_base, calculate_loyalty_discount,
#                              agent_core_browser.browser]
#   5. Connect to the Gateway via MCPClient, load gateway_tools, extend tools list
#   6. Create and invoke the Agent with all tools, hooks, and system_prompt
#   7. Return the text from the first content block of the response
#   8. Handle exceptions gracefully

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

@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    # TODO: Implement the agent invocation
    
    from strands_tools.browser import AgentCoreBrowser
    user_input = payload.get("prompt")
    if not user_input or not str(user_input).strip():
        return "I did not receive a question. What can I help you with?"

    actor_id = payload.get("customer_id")
    session_id = payload.get("session_id")
    if not session_id:
        session_id = uuid.uuid4()
    memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)
    agentborwser = AgentCoreBrowser(region=REGION)
    tools = [search_knowledge_base, calculate_loyalty_discount,agentborwser.browser]

    mcp_client = MCPClient(url=GATEWAY_URL)

    gateway_tools = []
    try:
        with mcp_client:
            gateway_tools = list(mcp_client.list_tools_sync())
            logger.info("Loaded %d gateway tool(s)", len(gateway_tools))
    except Exception as e:
        # Degrade rather than fail: knowledge base, discounts and the
        # browser still work without the gateway.
        logger.error("Gateway unavailable, continuing without it: %s", e)

    agent = Agent(
        model=model,
        tools=tools + gateway_tools,
        system_prompt=SYSTEM_PROMPT,
        hooks=[memory_hook],
    )

    result = await agent.invoke_async(user_input)

    for block in result.message.get("content", []):
        if "text" in block:
            return block["text"]      
 
    return "I could not produce a response. Please try rephrasing."
    


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
