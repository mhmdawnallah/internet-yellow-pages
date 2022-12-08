import logging
import sys
from datetime import datetime, time, timezone
from neo4j import GraphDatabase
from neo4j.exceptions import ConstraintError
from frozendict import frozendict
import functools

# Usual constraints on nodes' properties
NODE_CONSTRAINTS = {
        'AS': {
                'asn': set(['UNIQUE', 'NOT NULL'])
                } ,

        'PREFIX': {
                'prefix': set(['UNIQUE', 'NOT NULL']), 
                'af': set(['NOT NULL'])
                },
        
        'IP': {
                'ip': set(['UNIQUE', 'NOT NULL']),
                'af': set(['NOT NULL'])
                },

        'DOMAIN_NAME': {
                'name': set(['UNIQUE', 'NOT NULL'])
                },

        'COUNTRY': {
                'country_code': set(['UNIQUE', 'NOT NULL'])
                },

        'ORGANIZATION': {
                'name': set(['NOT NULL'])
                },
    }

# Properties that may be frequently queried and that are not constraints
NODE_INDEXES = {
        'PEERINGDB_ORG_ID': [ 'id' ]
        }

# Set of node labels with constrains (ease search for node merging)
NODE_CONSTRAINTS_LABELS = set(NODE_CONSTRAINTS.keys())

def format_properties(prop):
    """Make sure certain properties are always formatted the same way.
    For example IPv6 addresses are stored in lowercase, or ASN are kept as 
    integer not string."""

    prop = dict(prop)

    # asn is stored as an int
    if 'asn' in prop:
        prop['asn'] = int(prop['asn'])

    # ipv6 is stored in lowercase
    if 'ip' in prop:
        prop['ip'] = prop['ip'].lower()
    if 'prefix' in prop:
        prop['prefix'] = prop['prefix'].lower()

    # country code is kept in capital letter
    if 'country_code' in prop:
        prop['country_code'] = prop['country_code'].upper()

    return prop


def dict2str(d, eq=':', pfx=''):
    """Converts a python dictionary to a Cypher map."""

    data = [] 
    for key, value in d.items():
        if isinstance(value, str) and '"' in value:
            escaped = value.replace("'", r"\'")
            data.append(f"{pfx+key}{eq} '{escaped}'")
        elif isinstance(value, str) or isinstance(value, datetime):
            data.append(f'{pfx+key}{eq} "{value}"')
        else:
            data.append(f'{pfx+key}{eq} {value}')

    return '{'+','.join(data)+'}'


def freezeargs(func):
    """Transform mutable dictionnary
    Into immutable
    Useful to be compatible with cache
    """

    @functools.wraps(func)
    def wrapped(*args, **kwargs):
        args = tuple([frozendict(arg) if isinstance(arg, dict) else arg for arg in args])
        kwargs = {k: frozendict(v) if isinstance(v, dict) else v for k, v in kwargs.items()}
        return func(*args, **kwargs)
    return wrapped

class IYP(object):

    def __init__(self):

        logging.debug('IYP: Enter initialization')
        self.neo4j_enterprise = False

        # TODO: get config from configuration file
        self.server = 'localhost'
        self.port = 7687
        self.login = "neo4j"
        self.password = "password"

        # Connect to the database
        uri = f"neo4j://{self.server}:{self.port}"
        self.db = GraphDatabase.driver(uri, auth=(self.login, self.password))

        if self.db is None:
            sys.exit('Could not connect to the Neo4j database!')
        else:
            self.session = self.db.session()

        self._db_init()
        self.tx = self.session.begin_transaction()


    def _db_init(self):
        """Add constraints and indexes."""

        # Create constraints (implicitly add corresponding indexes)
        for label, prop_constraints in NODE_CONSTRAINTS.items():
            for property, constraints in prop_constraints.items():

                for constraint in constraints:
                    # neo4j-community only implements the UNIQUE constraint
                    if not self.neo4j_enterprise and constraint != 'UNIQUE':
                        continue

                    constraint_formated = constraint.replace(' ', '')
                    self.session.run(
                        f" CREATE CONSTRAINT {label}_{constraint_formated}_{property} IF NOT EXISTS "
                        f" FOR (n:{label}) "
                        f" REQUIRE n.{property} IS {constraint} ")

        # Create indexes
        for label, indexes in NODE_INDEXES.items():
            for index in indexes:
                self.session.run(
                    f" CREATE INDEX {label}_INDEX_{index} IF NOT EXISTS "
                    f" FOR (n:{label}) "
                    f" ON (n.{index}) ")

    def commit(self):
        """Commit all pending queries (node/link creation) and start a new
        transaction."""

        self.tx.commit()
        self.tx = self.session.begin_transaction()

    def rollback(self):
        """Rollback all pending queries (node/link creation) and start a new
        transaction."""

        self.tx.rollback()
        self.tx = self.session.begin_transaction()

    @freezeargs
    @functools.lru_cache(maxsize=100000) 
    def get_node(self, type, prop, create=False):
        """Find the ID of a node in the graph. Return None if the node does not
        exist or create the node if create=True."""

        prop = format_properties(prop)

        # put type in a list
        type_str = str(type)
        if isinstance(type, list):
            type_str = ':'.join(type)
        else:
            type = [type]

        if create:
            has_constraints = NODE_CONSTRAINTS_LABELS.intersection(type)
            if len( has_constraints ):
                ### MERGE node with constraints
                ### Search on the constraints and set other values
                label = has_constraints.pop()
                constraint_prop = dict([ (c, prop[c]) for c in NODE_CONSTRAINTS[label].keys() ]) 
                #values = ', '.join([ f"a.{p} = {val}" for p, val in prop.items() ])
                labels = ', '.join([ f"a:{l}" for l in type])

                # TODO: fix this. Not working as expected. e.g. getting prefix
                # with a descr in prop
                try:
                    result = self.tx.run(
                    f"""MERGE (a:{label} {dict2str(constraint_prop)}) 
                        ON MATCH
                            SET {dict2str(prop, eq='=', pfx='a.')[1:-1]}, {labels}
                        ON CREATE
                            SET {dict2str(prop, eq='=', pfx='a.')[1:-1]}, {labels}
                        RETURN ID(a)"""
                        ).single()
                except ConstraintError:
                    sys.stderr.write(f'cannot merge {prop}')
                    result = self.tx.run(
                    f"""MATCH (a:{label} {dict2str(constraint_prop)}) RETURN ID(a)""").single()

            else:
                ### MERGE node without constraints
                result = self.tx.run(f"MERGE (a:{type_str} {dict2str(prop)}) RETURN ID(a)").single()
        else:
            ### MATCH node
            result = self.tx.run(f"MATCH (a:{type_str} {dict2str(prop)}) RETURN ID(a)").single()

        if result is not None:
            return result[0]
        else:
            return None

    @functools.lru_cache(maxsize=100000)
    def get_node_extid(self, id_type, id):
        """Find a node in the graph which has an EXTERNAL_ID relationship with
        the given ID. Return None if the node does not exist."""

        result = self.tx.run(f"MATCH (a)-[:EXTERNAL_ID]->(:{id_type} {{id:{id}}}) RETURN ID(a)").single()

        if result is not None:
            return result[0]
        else:
            return None



    def add_links(self, src_node, links):
        """Create links from src_node to the destination nodes given in parameter
        links. This parameter is a list of [link_type, dst_node_id, prop_dict].
        The dictionary prop_dict should at least contain a 'source', 'point in time', 
        and 'reference URL'. Keys in this dictionary should contain no space.

        By convention link_type is written in UPPERCASE and keys in prop_dict are
        in lowercase."""

        matches = ' MATCH (x)' 
        where = f" WHERE ID(x) = {src_node}"
        merges = ''
        
        for i, (type, dst_node, prop) in enumerate(links):

            assert 'reference_org' in prop
            assert 'reference_url' in prop
            assert 'reference_time' in prop

            prop = format_properties(prop)

            matches += f", (x{i})"
            where += f" AND ID(x{i}) = {dst_node}"
            merges += f" MERGE (x)-[:{type}  {dict2str(prop)}]->(x{i}) "

        self.tx.run( matches+where+merges).consume()


    def close(self):
        """Commit pending queries and close IYP"""
        self.tx.commit()
        self.session.close()
        self.db.close()


class BaseCrawler(object):
    def __init__(self, organization, url):
        """IYP and references initialization"""

        self.reference = {
            'reference_org': organization,
            'reference_url': url,
            'reference_time': datetime.combine(datetime.utcnow(), time.min, timezone.utc)
            }

        # connection to IYP database
        self.iyp = IYP()
    
    
    def close(self):
        # Commit changes to IYP
        self.iyp.close()

