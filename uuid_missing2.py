import pymongo

def list_mongods_in_replicaset(rs_uri, rs):
    res = []
    mc_s = pymongo.MongoClient(rs_uri)
    status = mc_s.admin.command('replSetGetStatus')
    members = status['members']
    # TODO get rs from url or discover it
    for m in members:
        res.append({'rs': rs, 'url': f'mongodb://{m["name"]}/', 'state': m['stateStr']})
    return res

def list_mongods_in_cluster(mc):
    res = []
    for shard in mc.admin.command('listShards')['shards']:
        rs = shard['_id']
        rs_hosts = shard['host'].split('/')[1]
        rs_uri= f"mongodb://{rs_hosts}/?replicaSet={rs}"
        res.extend(list_mongods_in_replicaset(rs_uri, rs))
    return res

mc = pymongo.MongoClient("mongodb://nj1-fadthistle.flt:17021/?readPreference=secondary")
mongods = list_mongods_in_cluster(mc)

# All collections in config.collections are sharded.
for c in mc.config.collections.find({'dropped': False}):
    ns = c['_id']
    database, collection = ns.split('.', 1)
    if database == 'config':
        continue

    # Get the primary shard for the collection's database.
    database_doc = mc.config.databases.find_one({'_id': database})
    if not database_doc:
        continue
    primary_shard = database_doc['primary']

    for mongod in mongods:
        if mongod['state'] == 'PRIMARY':
            rs_primary = mongod['url']
            mongod_client = pymongo.MongoClient(rs_primary)
            cache_doc = mongod_client.config['cache.collections'].find_one({'_id': ns})
            if cache_doc:
                status = 'UUID_MISSING' if 'uuid' not in cache_doc else 'OK'
            else:
                status = 'NO_CACHE_DOC'

            is_primary_shard = 'is_primary_shard' if primary_shard == mongod['rs'] else 'not_primary_shard'

            print(ns, mongod['rs'], is_primary_shard, mongod['state'], mongod['url'], status)
