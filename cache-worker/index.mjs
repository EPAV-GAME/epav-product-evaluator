import { createClient } from 'redis';

const PREFIX = 'epav:game-cache:v1:';
const RELEASE = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end";

export async function operation(client, input) {
  const { op, key, value, ttl } = input;
  if (op === 'ping') return await client.ping() === 'PONG';
  if (op === 'invalidate') {
    await client.set(PREFIX + 'generation', crypto.randomUUID());
    return true;
  }
  if (typeof key !== 'string' || !/^[a-zA-Z0-9:_-]{1,180}$/.test(key)) throw new Error('Invalid key');
  const fullKey = PREFIX + key;
  if (op === 'get') return client.get(fullKey);
  if (op === 'read') {
    const [value, ttl_ms] = await client.multi().get(fullKey).pTTL(fullKey).exec();
    return { value, ttl_ms };
  }
  if (op === 'release') {
    if (typeof value !== 'string' || value.length > 80) throw new Error('Invalid lock');
    return client.eval(RELEASE, { keys: [fullKey], arguments: [value] });
  }
  if (!['set', 'claim'].includes(op) || !Number.isInteger(ttl) || ttl < 1 || ttl > 3600 ||
      typeof value !== 'string' || Buffer.byteLength(value) > 256000) throw new Error('Invalid operation');
  return (await client.set(fullKey, value, { EX: ttl, ...(op === 'claim' ? { NX: true } : {}) })) === 'OK';
}

export default {
  // Accessible exclusively through a Cloudflare service binding; no public route.
  async fetch(request, env) {
    if (request.method !== 'POST') return new Response(null, { status: 405 });
    if (Number(request.headers.get('content-length') || 0) > 300000) return new Response(null, { status: 413 });
    const text = await request.text();
    if (text.length > 300000) return new Response(null, { status: 413 });
    let input;
    try { input = JSON.parse(text); } catch { return new Response(null, { status: 400 }); }
    const client = createClient({
      username: env.REDIS_USERNAME, password: env.REDIS_PASSWORD,
      socket: { host: env.REDIS_HOST, port: Number(env.REDIS_PORT), tls: env.REDIS_TLS === 'true',
        connectTimeout: 1200, reconnectStrategy: false },
      disableOfflineQueue: true,
    });
    // Never log raw Redis errors: these may include connection details.
    client.on('error', () => {});
    let timer;
    try {
      const result = await Promise.race([
        (async () => { await client.connect(); return operation(client, input); })(),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Cache timeout')), 1800); }),
      ]);
      return Response.json({ value: result });
    } catch {
      return Response.json({ error: 'CACHE_UNAVAILABLE' }, { status: 503 });
    } finally {
      clearTimeout(timer);
      if (client.isOpen) client.destroy();
    }
  },
};
