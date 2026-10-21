// Minimal server on the REAL official TypeScript MCP SDK, with one tool that
// has a stray console.log() left in it -- the classic mistake this whole
// "stdout must be pure JSON-RPC" rule exists to prevent.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const server = new McpServer({ name: "broken-demo", version: "1.0.0" });

server.registerTool(
  "echo",
  {
    description: "Echoes back the input",
    inputSchema: { text: z.string() },
  },
  async ({ text }) => {
    console.log(`DEBUG: echo tool got: ${text}`); // <-- the stray debug print (BUG)
    return { content: [{ type: "text", text }] };
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);
