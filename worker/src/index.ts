export interface Env {
  RT_FEED: DurableObjectNamespace;
  DO_UPDATE_SECRET: string;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const id = env.RT_FEED.idFromName("global");
    const stub = env.RT_FEED.get(id);

    if (url.pathname === "/rt/ws" || url.pathname === "/rt/publish") {
      return stub.fetch(request);
    }

    return new Response("Not found", { status: 404 });
  },
};

/**
 * Single global Durable Object instance. Holds the most recent rt.pb bytes
 * and fans them out to every connected WebSocket client, so clients get
 * live updates without polling R2 directly.
 */
export class RtFeedBroadcaster {
  state: DurableObjectState;
  env: Env;

  constructor(state: DurableObjectState, env: Env) {
    this.state = state;
    this.env = env;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/rt/publish") {
      return this.handlePublish(request);
    }

    if (url.pathname === "/rt/ws") {
      return this.handleWebSocketUpgrade(request);
    }

    return new Response("Not found", { status: 404 });
  }

  private async handlePublish(request: Request): Promise<Response> {
    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405 });
    }

    const authHeader = request.headers.get("Authorization") || "";
    const expected = `Bearer ${this.env.DO_UPDATE_SECRET}`;
    if (!this.env.DO_UPDATE_SECRET || authHeader !== expected) {
      return new Response("Unauthorized", { status: 401 });
    }

    const body = await request.arrayBuffer();
    await this.state.storage.put("latest", body);

    let broadcastCount = 0;
    for (const ws of this.state.getWebSockets()) {
      try {
        ws.send(body);
        broadcastCount++;
      } catch {
        // Ignore sockets that fail to send; hibernation API will clean them up.
      }
    }

    return new Response(
      JSON.stringify({ ok: true, bytes: body.byteLength, clients: broadcastCount }),
      { status: 200, headers: { "Content-Type": "application/json" } }
    );
  }

  private async handleWebSocketUpgrade(request: Request): Promise<Response> {
    if (request.headers.get("Upgrade") !== "websocket") {
      return new Response("Expected websocket", { status: 426 });
    }

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    // Hibernatable API: the DO can be evicted between messages and woken back
    // up by an incoming event, instead of paying to stay resident for every
    // idle connection.
    this.state.acceptWebSocket(server);

    const latest = await this.state.storage.get<ArrayBuffer>("latest");
    if (latest) {
      server.send(latest);
    }

    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(_ws: WebSocket, _message: string | ArrayBuffer) {
    // Clients don't send data; nothing to do.
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string, wasClean: boolean) {
    ws.close(code, reason);
  }

  async webSocketError(ws: WebSocket, _error: unknown) {
    ws.close();
  }
}