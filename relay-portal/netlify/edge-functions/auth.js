// Basic Auth gate for the Relay portal.
export default async (request, context) => {
  const user =
    Deno.env.get("RELAY_BASIC_USER") ||
    Deno.env.get("APP_USERNAME") ||
    "";
  const pass =
    Deno.env.get("RELAY_BASIC_PASS") ||
    Deno.env.get("APP_PASSWORD") ||
    "";
  if (!user || !pass) {
    return new Response("Portal auth env not configured (RELAY_BASIC_USER/PASS)", {
      status: 500,
    });
  }

  const header = request.headers.get("authorization") || "";
  if (header.startsWith("Basic ")) {
    try {
      const decoded = atob(header.slice(6));
      const idx = decoded.indexOf(":");
      const u = decoded.slice(0, idx);
      const p = decoded.slice(idx + 1);
      if (u === user && p === pass) {
        return context.next();
      }
    } catch {
      // challenge
    }
  }

  return new Response("Authentication required", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Relay"',
      "Cache-Control": "no-store",
    },
  });
};
