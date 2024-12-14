import nbformat as nbf

# Create a new notebook
nb = nbf.v4.new_notebook()

# Add markdown cell explaining the notebook
nb.cells.append(
    nbf.v4.new_markdown_cell(
        """# Voyage AI Integration Example with AgentOps

This notebook demonstrates how to use the Voyage AI provider with AgentOps for embedding operations. The integration supports both synchronous and asynchronous operations, includes token usage tracking, and provides proper error handling.

## Requirements
- Python >= 3.9 (Voyage AI SDK requirement)
- AgentOps library
- Voyage AI API key"""
    )
)

# Add cell for imports and setup
nb.cells.append(
    nbf.v4.new_code_cell(
        """import os
import asyncio
import agentops
import voyageai
from agentops.llms.providers.voyage import VoyageProvider

# Check for API keys
if "AGENTOPS_API_KEY" not in os.environ:
    print("Note: AGENTOPS_API_KEY not set. Using mock session for demonstration.")
    print("Please set your AgentOps API key in the environment variables")

if "VOYAGE_API_KEY" not in os.environ:
    print("Note: VOYAGE_API_KEY not set. Using mock client for demonstration.")
    print("Please set your Voyage AI API key in the environment variables")

# Initialize AgentOps client and start session
ao_client = agentops.Client()
session = ao_client.initialize()
if session is None:
    print("Failed to initialize AgentOps client")
    raise RuntimeError("AgentOps client initialization failed")

# Initialize Voyage AI client
try:
    voyage_client = voyageai.Client()
    provider = VoyageProvider(voyage_client)
    print("Successfully initialized Voyage AI provider")
except Exception as e:
    print(f"Failed to initialize Voyage AI provider: {e}")
    raise

print(f"AgentOps Session URL: {session.session_url}")"""
    )
)

# Add cell for basic embedding
nb.cells.append(
    nbf.v4.new_code_cell(
        """# Example text for embedding
text = "The quick brown fox jumps over the lazy dog."

try:
    # Generate embeddings with session tracking
    result = provider.embed(text, session=session)
    print(f"Embedding dimension: {len(result['embeddings'][0])}")
    print(f"Token usage: {result['usage']}")
except Exception as e:
    print(f"Failed to generate embeddings: {e}")
    raise"""
    )
)

# Add cell for async embedding
nb.cells.append(
    nbf.v4.new_code_cell(
        """async def process_multiple_texts():
    texts = [
        "First example text",
        "Second example text",
        "Third example text"
    ]

    try:
        # Process texts concurrently with session tracking
        tasks = [provider.aembed(text, session=session) for text in texts]
        results = await asyncio.gather(*tasks)

        # Display results
        for i, result in enumerate(results, 1):
            print(f"\\nText {i}:")
            print(f"Embedding dimension: {len(result['embeddings'][0])}")
            print(f"Token usage: {result['usage']}")

        return results
    except Exception as e:
        print(f"Failed to process texts: {e}")
        raise

# Run async example
results = await process_multiple_texts()"""
    )
)

# Add cell for cleanup
nb.cells.append(
    nbf.v4.new_code_cell(
        """# End the session
ao_client.end_session("Success", "Example notebook completed successfully")"""
    )
)

# Write the notebook
with open("voyage_example.ipynb", "w") as f:
    nbf.write(nb, f)

print("Notebook created successfully!")
