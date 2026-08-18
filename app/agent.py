import os

from strands import Agent
from strands import tool
from strands.models import BedrockModel
from bedrock_agentcore import BedrockAgentCoreApp
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

load_dotenv()

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
DRY_RUN = os.environ.get("DRY_RUN", "false").lower() == "true"
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "eu.amazon.nova-micro-v1:0")

app = BedrockAgentCoreApp()

model = BedrockModel(
    model_id=MODEL_ID,
    #model_id="eu.anthropic.claude-opus-4-6-v1",
    region_name=AWS_REGION,
)

SYSTEM_PROMPT = """You are OpsSherlock, a helpful SRE assistant. 
    You will be given a prompt from a user, and you will respond with a helpful answer.
    """

# ---------------------------------------------------------------------------
# Sub-Agents wrapped as Tools (Agents-as-Tools pattern)
# ---------------------------------------------------------------------------

_rca_agent = Agent(
    model=model,
    system_prompt="""You are a senior Site Reliability Engineer performing root cause analysis.
    Given alarm data, metrics, and log snippets, your job is to:
    1. Identify the most likely root cause(s).
    2. Assess the blast radius (which services/users are affected).
    3. Rate the severity (P1 critical / P2 high / P3 medium).
    4. Propose 2-3 concrete remediation options ranked by risk.
    
    Be precise. Use technical language. Cite specific metric values and log lines.
    """
)

_remediation_agent = Agent(
    model=model,
    system_prompt="""You are a Kubernetes and Helm operations expert.
    Given a root cause analysis, your job is to:
    1. Inspect the current state of affected workloads with kubectl.
    2. Propose and execute the safest remediation action (rollback, restart, scale).
    3. Always prefer reversible actions (rollback > restart > scale).
    4. Confirm the action taken or explain why no action was taken.
    
    In DRY_RUN mode, commands are simulated and safe to run.
    """
)

@tool
def rca_agent(context: str) -> str:
    response = _rca_agent(context)
    return str(response)

@tool
def remediation_agent(instructions: str) -> str:
    response = _remediation_agent(instructions)
    return str(response)

# ---------------------------------------------------------------------------
# Main agent
# ---------------------------------------------------------------------------

agent = Agent(
    model=model,
    system_prompt=SYSTEM_PROMPT,
    tools=[
        rca_agent,
        remediation_agent,
    ],
)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@app.entrypoint
async def agent_invocation(payload):
    """Handler for agent invocation"""
    user_message = payload.get(
        "prompt", "No prompt found in input, please guide customer to create a json payload with prompt key"
    )
    stream = agent.stream_async(user_message)
    async for event in stream:
        print(event)
        yield event


if __name__ == "__main__":
    app.run()
