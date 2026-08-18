from strands import Agent
from strands.models import BedrockModel
from bedrock_agentcore import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

model = BedrockModel(
    model_id="eu.amazon.nova-micro-v1:0",
    #model_id="eu.anthropic.claude-opus-4-6-v1",
    region_name="eu-central-1",
)

agent = Agent(model=model)


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
