import {readFile,writeFile} from 'node:fs/promises';
import vm from 'node:vm';
const sourcePath = process.argv[2];
if (!sourcePath) throw new Error('Informe o caminho de clientsData.js do jogo');
const source = await readFile(sourcePath,'utf8');
const context = vm.createContext({});
vm.runInContext(source+';globalThis.scenarios=clientes;',context,{timeout:1000});
const revisedPath=process.argv[3];
if(revisedPath)vm.runInContext(await readFile(revisedPath,'utf8'),context,{timeout:1000});
const scenarios = revisedPath ? Object.fromEntries(Object.entries(context.EpavRoteirosV2.banco.clientes).map(([id,c])=>[id,{
  id,nome:c.nome,perfil:c.perfil,restricoes:c.restricoes,noInicial:'d1',
  satisfacaoInicial:context.scenarios.find(client=>client.id===id).satisfacaoInicial,
  dialogo:Object.fromEntries(c.nos.map(n=>[n.id,{...n,texto:n.cliente,retorno:null}]))
}])) : Object.fromEntries(context.scenarios.map(client=>[client.id,{
  id:client.id,nome:client.nome,perfil:client.perfil,objetivo:client.objetivo,licao:client.licao,
  noInicial:client.noInicial,satisfacaoInicial:client.satisfacaoInicial,
  dialogo:client.dialogo
}]));
const filename=revisedPath?'scenarios_v2.py':'scenarios.py';
const variable=revisedPath?'SCENARIOS_V2':'SCENARIOS';
await writeFile(new URL('../src/'+filename,import.meta.url),
  '# Gerado dos cenários oficiais do jogo; atualizar pelo script export_scenarios.mjs.\nimport json\n'+variable+' = json.loads('+JSON.stringify(JSON.stringify(scenarios))+')\n');
console.log(`Exportados ${Object.keys(scenarios).length} cenários oficiais do jogo.`);
