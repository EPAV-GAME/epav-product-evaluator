import {readFile,writeFile} from 'node:fs/promises';
import vm from 'node:vm';
const sourcePath = process.argv[2];
if (!sourcePath) throw new Error('Informe o caminho de clientsData.js do jogo');
const source = await readFile(sourcePath,'utf8');
const context = vm.createContext({});
vm.runInContext(source+';globalThis.scenarios=clientes;',context,{timeout:1000});
const scenarios = Object.fromEntries(context.scenarios.map(client=>[client.id,{
  id:client.id,nome:client.nome,perfil:client.perfil,objetivo:client.objetivo,licao:client.licao,
  noInicial:client.noInicial,satisfacaoInicial:client.satisfacaoInicial,
  dialogo:client.dialogo
}]));
await writeFile(new URL('../src/scenarios.py',import.meta.url),
  '# Gerado dos cenários oficiais do jogo; atualizar pelo script export_scenarios.mjs.\nimport json\nSCENARIOS = json.loads('+JSON.stringify(JSON.stringify(scenarios))+')\n');
console.log(`Exportados ${Object.keys(scenarios).length} cenários oficiais do jogo.`);
