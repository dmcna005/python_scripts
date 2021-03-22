// flush router table to avoid returning stale distribution databases
db.adminCommand( { flushRouterConfig: "test.myShardedCollection" } );

db.adminCommand('listDatabases').databases.forEach(function(d) {
  mdb = db.getSiblingDB(d.name);
  var colNames = mdb.getCollectionNames();
  colNames.forEach(function(c) {
    printjson(db.c.getShardDistribution();
  })

})

db.adminCommand('listDatabases').databases.forEach(function(d) {
  mdb = db.getSiblingDB(d.name);
  mdb.getCollectionNames().forEach(function(c) {
    // print(c);
    try {
        mdb[c].getShardDistribution();
    }
    catch(error) {
      print(c)
    }
    // s = mdb[c].stats();
    // printjson(s);
    // printjson(db.c.getShardDistribution();
  })

})
