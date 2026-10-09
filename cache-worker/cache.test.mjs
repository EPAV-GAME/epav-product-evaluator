import test from 'node:test';
import assert from 'node:assert/strict';
import { operation } from './index.mjs';
import { encodeCommand, parseReply, NativeRedis } from './resp.mjs';

test('commands are scoped; locks use NX and expiry; release checks owner atomically', async () => {
  let args;
  const client = { set: async (...a) => { args=a; return 'OK'; }, eval: async (...a) => { args=a; return 1; } };
  assert.equal(await operation(client,{op:'claim',key:'lock:a',value:'owner',ttl:30}),true);
  assert.deepEqual(args,['epav:game-cache:v1:lock:a','owner',{EX:30,NX:true}]);
  await operation(client,{op:'release',key:'lock:a',value:'owner'});
  assert.match(args[0],/ARGV\[1\]/);
  assert.deepEqual(args[1],{keys:['epav:game-cache:v1:lock:a'],arguments:['owner']});
});

test('rejects arbitrary commands, oversized values and keys outside allowed namespace', async () => {
  for (const input of [{op:'FLUSHALL',key:'a'},{op:'get',key:'../other'},{op:'set',key:'a',value:'x',ttl:0},
                       {op:'set',key:'a',value:'x'.repeat(256001),ttl:60}]) {
    await assert.rejects(operation({},input));
  }
});

test('large catalogs use the byte-buffer TCP path without changing content or expiry', async () => {
  let args;
  const value = 'á'.repeat(36000);
  await operation({set:async (...a) => {args=a;return 'OK';}}, {op:'set',key:'catalog-test',value,ttl:900});
  assert.ok(Buffer.isBuffer(args[1]));
  assert.equal(args[1].toString('utf8'),value);
  assert.deepEqual(args[2],{EX:900});
});

test('native RESP preserves Unicode byte lengths, fragmented replies and atomic TTL reads', async () => {
  const text = new TextDecoder().decode(encodeCommand(['SET','k','á']));
  assert.equal(text,'*3\r\n$3\r\nSET\r\n$1\r\nk\r\n$2\r\ná\r\n');
  const bytes=new TextEncoder().encode('*2\r\n$2\r\ná\r\n:900000\r\n');
  for (let i=0;i<bytes.length;i++) assert.equal(parseReply(bytes.subarray(0,i)),null);
  assert.deepEqual(parseReply(bytes).value,['á',900000]);
  assert.deepEqual(parseReply(new TextEncoder().encode('$-1\r\n')).value,null);
  assert.throws(()=>parseReply(new TextEncoder().encode('-ERR secret-detail\r\n')), {message:'Redis command failed'});
  const writes=[],replyBytes=new TextEncoder().encode('+OK\r\n+OK\r\n+QUEUED\r\n+QUEUED\r\n*2\r\n$2\r\ná\r\n:900000\r\n');
  let sent=false;
  const socket={opened:Promise.resolve(),close:async()=>{},writable:{getWriter:()=>({write:async bytes=>writes.push(bytes)})},
    readable:{getReader:()=>({read:async()=>sent?{done:true}:(sent=true,{done:false,value:replyBytes})})}};
  const client=new NativeRedis(()=>socket,{REDIS_HOST:'test',REDIS_PORT:'1',REDIS_USERNAME:'u',REDIS_PASSWORD:'p'});
  await client.connect();assert.deepEqual(await client.multi().get('k').pTTL('k').exec(),['á',900000]);
  assert.equal(writes.length,5);client.destroy();
});
