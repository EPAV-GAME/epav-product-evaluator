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
 records=await db.records('produtos_swift',limit=1)
 original=records[0]
 queries=[db.put_query('produtos_swift',original['id'],original['fields'],original['revision']),
          db.put_query('produtos_swift',original['id'],original['fields'],original['revision'])]
 try: await db.execute(queries,batch=True)
 except ServiceError as error:
  assert error.code=='CONFLICT'
 else: raise AssertionError('Expected rollback')
 restored=(await db.records('produtos_swift',id=original['id'],limit=1))[0]
 assert restored['revision']==original['revision'] and restored['fields']==original['fields']
 print('Atomic conflict rolled back every write: OK')
asyncio.run(main())
