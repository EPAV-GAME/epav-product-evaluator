import test from 'node:test';
import assert from 'node:assert/strict';
import { operation } from './index.mjs';

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
