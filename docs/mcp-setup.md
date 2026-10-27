# MCP servers for GuardIA

This project gives the Kiro agent two MCP (Model Context Protocol) servers, configured at workspace level in [`.kiro/settings/mcp.json`](../.kiro/settings/mcp.json). Because the file lives in the repository, every team member gets the same tools.

| Server | What it gives the agent | How we use it |
|---|---|---|
| `aws-docs` | Search and read the official AWS documentation | Check Step Functions (redrive, Wait states), EventBridge, Bedrock and DynamoDB details before writing code or changing the spec |
| `github` | Read and write issues and pull requests in this repository | Create one issue per task in `.kiro/specs/guardia/tasks.md`, assign it to a team member, and link PRs with `Closes #N` |

## Setup (once per person)

1. **Install `uv`** (it provides `uvx`, which runs the AWS docs server): <https://docs.astral.sh/uv/getting-started/installation/>
2. **Create a GitHub token** (GitHub → Settings → Developer settings → Personal access tokens → Fine-grained). Limit it to this repository and grant read/write on *Issues* and *Pull requests*, plus read on *Contents*.
3. **Export it** in the environment where Kiro starts, never in the repository:
   - macOS/Linux: `export GITHUB_TOKEN=<your-token>` and launch Kiro from that terminal.
   - Windows (PowerShell): `setx GITHUB_TOKEN "<your-token>"`, then restart Kiro.
4. **Approve the variable in Kiro**: Settings → search for *Mcp Approved Env Vars* → add `GITHUB_TOKEN`. Kiro only expands variables you approve.
5. **Check the connection**: open the MCP panel in Kiro; both servers must show as connected.

## Security rules

- Tokens are referenced as `${GITHUB_TOKEN}`; the real value is never committed (see `.kiro/steering/tech.md`).
- The `github` server has no `autoApprove`, so Kiro asks before creating or changing anything in the repository.
- Use a fine-grained token limited to this repository, and revoke it when the hackathon ends.

## Optional servers

The AWS Labs project publishes more MCP servers (infrastructure as code, pricing, diagrams). Check <https://awslabs.github.io/mcp/> for the current names before adding one to `mcp.json`.
