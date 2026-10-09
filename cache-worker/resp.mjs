// Small RESP2 adapter for the native Cloudflare TCP socket. No reconnection or
// raw server error logging; callers expose only the fixed cache operations.
const encoder = new TextEncoder(), decoder = new TextDecoder();
export function encodeCommand(args) {
  const parts = [encoder.encode(`*${args.length}\r\n`)];
  for (const arg of args) {
    const bytes = typeof arg === 'string' ? encoder.encode(arg) : arg;
    parts.push(encoder.encode(`$${bytes.byteLength}\r\n`), bytes, encoder.encode('\r\n'));
  }
  const output = new Uint8Array(parts.reduce((size,part) => size+part.byteLength,0));
  let offset=0;for (const part of parts) {output.set(part,offset);offset+=part.byteLength;}
  return output;
}

export function parseReply(bytes, start=0) {
  if (start>=bytes.length) return null;
  let end=start+1;
  while (end+1<bytes.length && !(bytes[end]===13 && bytes[end+1]===10)) end++;
  if (end+1>=bytes.length) return null;
  const text=decoder.decode(bytes.subarray(start+1,end));let next=end+2;
  if (bytes[start]===43) return {value:text,next};
  if (bytes[start]===45) throw new Error('Redis command failed');
  const size=Number(text);
  if (!Number.isSafeInteger(size)) throw new Error('Invalid Redis response');
  if (bytes[start]===58) return {value:size,next};
  if (size===-1 && (bytes[start]===36 || bytes[start]===42)) return {value:null,next};
  if (size<0 || size>300000) throw new Error('Invalid Redis response');
  if (bytes[start]===36) {
    if (bytes.length<next+size+2) return null;
    if (bytes[next+size]!==13 || bytes[next+size+1]!==10) throw new Error('Invalid Redis response');
    return {value:decoder.decode(bytes.subarray(next,next+size)),next:next+size+2};
  }
  if (bytes[start]===42) {
    if (size>32) throw new Error('Invalid Redis response');
    const value=[];
    for (let i=0;i<size;i++) {
      const parsed=parseReply(bytes,next);if (!parsed) return null;
      value.push(parsed.value);next=parsed.next;
    }
    return {value,next};
  }
  throw new Error('Invalid Redis response');
}

export class NativeRedis {
  constructor(connect, env) {this.connectSocket=connect;this.env=env;this.pending=new Uint8Array();this.isOpen=false;}
  async connect() {
    this.socket=this.connectSocket({hostname:this.env.REDIS_HOST,port:Number(this.env.REDIS_PORT)},
      {secureTransport:this.env.REDIS_TLS==='true'?'on':'off'});
    this.isOpen=true;this.reader=this.socket.readable.getReader();this.writer=this.socket.writable.getWriter();
    await this.socket.opened;
    await this.command(['AUTH',this.env.REDIS_USERNAME,this.env.REDIS_PASSWORD]);
  }
  async reply() {
    while (true) {
      const parsed=parseReply(this.pending);
      if (parsed) {this.pending=this.pending.slice(parsed.next);return parsed.value;}
      const {value,done}=await this.reader.read();if (done) throw new Error('Redis socket closed');
      if (this.pending.length+value.length>300000) throw new Error('Invalid Redis response');
      const merged=new Uint8Array(this.pending.length+value.length);
      merged.set(this.pending);merged.set(value,this.pending.length);this.pending=merged;
    }
  }
  async command(args) {
    const bytes=encodeCommand(args);
    for (let offset=0;offset<bytes.length;offset+=16384) await this.writer.write(bytes.subarray(offset,offset+16384));
    return this.reply();
  }
  ping() {return this.command(['PING']);}
  get(key) {return this.command(['GET',key]);}
  set(key,value,options={}) {
    return this.command(['SET',key,value,...(options.EX?['EX',String(options.EX)]:[]),...(options.NX?['NX']:[])]);
  }
  eval(script,{keys,arguments:args}) {return this.command(['EVAL',script,String(keys.length),...keys,...args]);}
  multi() {
    const commands=[];const client=this;
    return {get(key) {commands.push(['GET',key]);return this;},pTTL(key) {commands.push(['PTTL',key]);return this;},
      async exec() {
        // Pipeline the transaction in one network exchange.
        for (const command of [['MULTI'],...commands,['EXEC']]) await client.writer.write(encodeCommand(command));
        for (let i=0;i<commands.length+1;i++) await client.reply();
        return client.reply();
      }};
  }
  destroy() {this.isOpen=false;this.socket?.close().catch(()=>{});}
}
