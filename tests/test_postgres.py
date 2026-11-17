import json
import unittest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from main import app
from postgres import Postgres, flatten, rebuild, encode
from services import ServiceError, HTTPResult, FirebaseService
from data_routes import validate_edit, TYPES, OCCASIONS

class TransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_query_parameters_are_separate_and_errors_are_redacted(self):
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps({'rows':[['{"ok":true}']]})))
        db=Postgres('postgresql://user:secret@ep-example-pooler.us-east-2.aws.neon.tech/neondb',transport)
        self.assertEqual(await db.query('SELECT to_jsonb($1::varchar)',["';DROP TABLE ranking;--"]),[{'ok':True}])
        self.assertEqual(transport.call_args.kwargs['payload']['params'],["';DROP TABLE ranking;--"])
        transport.return_value=HTTPResult(400,{},json.dumps({'code':'40001','message':'secret'}))
        with self.assertRaises(ServiceError) as error: await db.query('SELECT 1')
        self.assertEqual((error.exception.status,str(error.exception)),(409,'CONFLICT'))
        transport.return_value=HTTPResult(500,{},'private details')
        with self.assertRaises(ServiceError) as error: await db.query('SELECT 1')
        self.assertNotIn('private',str(error.exception))

    async def test_batch_is_one_serializable_transaction(self):
        transport=AsyncMock(return_value=HTTPResult(200,{},json.dumps({'results':[{'rows':[['true']]},{'rows':[['"new"']]}]})))
        db=Postgres('postgres://u:p@ep-example.neon.tech/db',transport)
        await db.execute([{'query':'SELECT true','params':[]},db.put_query('c','id',{},'old')],batch=True)
        self.assertEqual(transport.call_count,1)
        self.assertEqual(transport.call_args.kwargs['headers']['Neon-Batch-Isolation-Level'],'Serializable')

class TypesTest(unittest.TestCase):
    def test_typed_roundtrip_keeps_int64_timestamp_reference_bytes_and_empty_maps(self):
        fields={'i':{'integerValue':'9223372036854775807'},'t':{'timestampValue':'2026-11-17T12:00:00.123456Z'},
            'r':{'referenceValue':'projects/p/databases/(default)/documents/c/d'},'b':{'bytesValue':'AAE='},
            'empty':{'mapValue':{}},'list':{'arrayValue':{'values':[encode(True),encode(None),encode({'a':'b'})]}}}
        self.assertEqual(rebuild(flatten(fields)),fields)

    def test_admin_edit_preserves_private_source_and_rejects_unknown_categories(self):
        original=dict(nome='Produto',disponivelNoJogo=True,tiposProduto=['Carnes'],ocasioes=['Dia a dia'],
            dadosOriginais={'Margem':99,'Marca':'Swift'})
        updated=validate_edit(original,dict(nome='Novo',dadosOriginais={'Margem':0}))
        self.assertEqual(updated['dadosOriginais']['Margem'],99)
        self.assertEqual(updated['dadosOriginais']['Descricao Produto'],'Novo')
        with self.assertRaises(ServiceError):validate_edit(original,{'tiposProduto':['inventado']})
        with self.assertRaises(ServiceError):validate_edit(original,{'imagemSwift':{'url':'https://outside.example/photo'}})
        with self.assertRaises(ServiceError):validate_edit(original,{'fornecedor':'X'})

class RoutesTest(unittest.TestCase):
    def test_anonymous_admin_bot_and_ranking_writes_are_denied(self):
        with TestClient(app) as client:
            self.assertEqual(client.get('/v1/admin/dados/produtos_swift').status_code,401)
            self.assertEqual(client.get('/v1/sync/dados/produtos_swift').status_code,403)
            self.assertEqual(client.post('/v1/sync/patch',json={}).status_code,403)
            self.assertEqual(client.post('/v1/sync/archive',json={}).status_code,403)
            self.assertEqual(client.post('/v1/sync/restore',json={}).status_code,403)
            self.assertEqual(client.post('/v1/ranking',json=dict(nome='João',pontos=0,qualidadeQuartos=0,
                satisfacao=0,classificacao='A',tempoJogadoMs=0)).status_code,401)

    def test_nonadmin_is_denied_before_any_sql(self):
        with patch.object(FirebaseService,'authenticate_admin',AsyncMock(side_effect=ServiceError('ADMIN_REQUIRED',403))), \
             patch.object(FirebaseService,'database',side_effect=AssertionError('No database before admin check')),TestClient(app) as client:
            self.assertEqual(client.get('/v1/admin/dados/produtos_swift',headers={'Authorization':'Bearer '+'t'*30}).status_code,403)

    def test_edit_conflict_never_commits_audit_or_product(self):
        db=type('DB',(),{'records':AsyncMock(return_value=[dict(id='p',revision='current',fields={})]),
            'execute':AsyncMock()})()
        with patch.object(FirebaseService,'authenticate_admin',AsyncMock(return_value={'localId':'admin'})), \
             patch.object(FirebaseService,'database',return_value=db),TestClient(app) as client:
            response=client.post('/v1/admin/produtos/p',json={'version':'old','changes':{}},
                headers={'Authorization':'Bearer '+'t'*30})
            self.assertEqual(response.status_code,409);db.execute.assert_not_called()
