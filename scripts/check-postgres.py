import asyncio,os
from services import FirebaseService
async def main():
 service=FirebaseService('unused',database_url=os.environ['DATABASE_URL'])
 db=service.database()
 rows=await db.catalog();print('Postgres catalog products:',len(rows))
 page=await db.records('produtos_swift',limit=2);print('Typed records reconstructed:',len(page))
 menu=await service._menu_catalog();print('Public photo catalog:',len(menu))
 print('Ranking:',len(await db.ranking()))
 from postgres import encode
 from services import ServiceError
 queries=[db.put_query('_migration_probe','rollback',{'n':encode(1)},''),
          db.put_query('_migration_probe','rollback',{'n':encode(2)},'')]
 try: await db.execute(queries,batch=True)
 except ServiceError as error:
  assert error.code=='CONFLICT'
 else: raise AssertionError('Expected rollback')
 assert not await db.records('_migration_probe',id='rollback',limit=1)
 print('Atomic conflict rolled back every write: OK')
asyncio.run(main())
