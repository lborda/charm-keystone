#!/usr/bin/python
import glob
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import subprocess
import threading
import time
import urlparse
import uuid
import yaml

from base64 import b64encode
from collections import OrderedDict
from copy import deepcopy

from charmhelpers.contrib.hahelpers.cluster import(
    is_elected_leader,
    determine_api_port,
    https,
    peer_units,
)

from charmhelpers.contrib.openstack import context, templating
from charmhelpers.contrib.network.ip import (
    is_ipv6,
    get_ipv6_addr
)

from charmhelpers.contrib.openstack.ip import (
    resolve_address,
    PUBLIC,
    INTERNAL,
    ADMIN
)

from charmhelpers.contrib.openstack.utils import (
    configure_installation_source,
    error_out,
    get_os_codename_install_source,
    git_install_requested,
    git_clone_and_install,
    os_release,
    save_script_rc as _save_script_rc)

from charmhelpers.core.host import (
    mkdir,
    write_file,
)

from charmhelpers.core.strutils import (
    bool_from_string,
)

import charmhelpers.contrib.unison as unison

from charmhelpers.core.decorators import (
    retry_on_exception,
)

from charmhelpers.core.hookenv import (
    charm_dir,
    config,
    is_relation_made,
    log,
    local_unit,
    relation_get,
    relation_set,
    relation_id,
    relation_ids,
    related_units,
    DEBUG,
    INFO,
    WARNING,
    ERROR,
)

from charmhelpers.fetch import (
    apt_install,
    apt_update,
    apt_upgrade,
    add_source,
)

from charmhelpers.core.host import (
    adduser,
    add_group,
    add_user_to_group,
    mkdir,
    service_stop,
    service_start,
    service_restart,
    pwgen,
    lsb_release,
    write_file,
)

from charmhelpers.contrib.peerstorage import (
    peer_store_and_set,
    peer_store,
    peer_retrieve,
)

from charmhelpers.core.templating import render

import keystone_context
import keystone_ssl as ssl

TEMPLATES = 'templates/'

# removed from original: charm-helper-sh
BASE_PACKAGES = [
    'apache2',
    'haproxy',
    'openssl',
    'python-keystoneclient',
    'python-mysqldb',
    'python-psycopg2',
    'python-six',
    'pwgen',
    'unison',
    'uuid',
]

BASE_GIT_PACKAGES = [
    'libxml2-dev',
    'libxslt1-dev',
    'python-dev',
    'python-pip',
    'python-setuptools',
    'zlib1g-dev',
]

BASE_SERVICES = [
    'keystone',
]

# ubuntu packages that should not be installed when deploying from git
GIT_PACKAGE_BLACKLIST = [
    'keystone',
    'python-keystoneclient',
]

API_PORTS = {
    'keystone-admin': config('admin-port'),
    'keystone-public': config('service-port')
}

KEYSTONE_CONF = "/etc/keystone/keystone.conf"
KEYSTONE_LOGGER_CONF = "/etc/keystone/logging.conf"
KEYSTONE_CONF_DIR = os.path.dirname(KEYSTONE_CONF)
STORED_PASSWD = "/var/lib/keystone/keystone.passwd"
STORED_TOKEN = "/var/lib/keystone/keystone.token"
SERVICE_PASSWD_PATH = '/var/lib/keystone/services.passwd'

HAPROXY_CONF = '/etc/haproxy/haproxy.cfg'
APACHE_CONF = '/etc/apache2/sites-available/openstack_https_frontend'
APACHE_24_CONF = '/etc/apache2/sites-available/openstack_https_frontend.conf'

APACHE_SSL_DIR = '/etc/apache2/ssl/keystone'
SYNC_FLAGS_DIR = '/var/lib/keystone/juju_sync_flags/'
SSL_DIR = '/var/lib/keystone/juju_ssl/'
SSL_CA_NAME = 'Ubuntu Cloud'
CLUSTER_RES = 'grp_ks_vips'
SSH_USER = 'juju_keystone'
SSL_SYNC_SEMAPHORE = threading.Semaphore()

BASE_RESOURCE_MAP = OrderedDict([
    (KEYSTONE_CONF, {
        'services': BASE_SERVICES,
        'contexts': [keystone_context.KeystoneContext(),
                     context.SharedDBContext(ssl_dir=KEYSTONE_CONF_DIR),
                     context.PostgresqlDBContext(),
                     context.SyslogContext(),
                     keystone_context.HAProxyContext(),
                     context.BindHostContext(),
                     context.WorkerConfigContext()],
    }),
    (KEYSTONE_LOGGER_CONF, {
        'contexts': [keystone_context.KeystoneLoggingContext()],
        'services': BASE_SERVICES,
    }),
    (HAPROXY_CONF, {
        'contexts': [context.HAProxyContext(singlenode_mode=True),
                     keystone_context.HAProxyContext()],
        'services': ['haproxy'],
    }),
    (APACHE_CONF, {
        'contexts': [keystone_context.ApacheSSLContext()],
        'services': ['apache2'],
    }),
    (APACHE_24_CONF, {
        'contexts': [keystone_context.ApacheSSLContext()],
        'services': ['apache2'],
    }),
])

CA_CERT_PATH = '/usr/local/share/ca-certificates/keystone_juju_ca_cert.crt'

valid_services = {
    "nova": {
        "type": "compute",
        "desc": "Nova Compute Service"
    },
    "nova-volume": {
        "type": "volume",
        "desc": "Nova Volume Service"
    },
    "cinder": {
        "type": "volume",
        "desc": "Cinder Volume Service"
    },
    "ec2": {
        "type": "ec2",
        "desc": "EC2 Compatibility Layer"
    },
    "glance": {
        "type": "image",
        "desc": "Glance Image Service"
    },
    "s3": {
        "type": "s3",
        "desc": "S3 Compatible object-store"
    },
    "swift": {
        "type": "object-store",
        "desc": "Swift Object Storage Service"
    },
    "quantum": {
        "type": "network",
        "desc": "Quantum Networking Service"
    },
    "oxygen": {
        "type": "oxygen",
        "desc": "Oxygen Cloud Image Service"
    },
    "ceilometer": {
        "type": "metering",
        "desc": "Ceilometer Metering Service"
    },
    "heat": {
        "type": "orchestration",
        "desc": "Heat Orchestration API"
    },
    "heat-cfn": {
        "type": "cloudformation",
        "desc": "Heat CloudFormation API"
    },
    "image-stream": {
        "type": "product-streams",
        "desc": "Ubuntu Product Streams"
    }
}


def filter_null(settings, null='__null__'):
    """Replace null values with None in provided settings dict.

    When storing values in the peer relation, it might be necessary at some
    future point to flush these values. We therefore need to use a real
    (non-None or empty string) value to represent an unset settings. This value
    then needs to be converted to None when applying to a non-cluster relation
    so that the value is actually unset.
    """
    filtered = {}
    for k, v in settings.iteritems():
        if v == null:
            filtered[k] = None
        else:
            filtered[k] = v

    return filtered


def resource_map():
    """Dynamically generate a map of resources that will be managed for a
    single hook execution.
    """
    resource_map = deepcopy(BASE_RESOURCE_MAP)

    if os.path.exists('/etc/apache2/conf-available'):
        resource_map.pop(APACHE_CONF)
    else:
        resource_map.pop(APACHE_24_CONF)
    return resource_map


def register_configs():
    release = os_release('keystone')
    configs = templating.OSConfigRenderer(templates_dir=TEMPLATES,
                                          openstack_release=release)
    for cfg, rscs in resource_map().iteritems():
        configs.register(cfg, rscs['contexts'])
    return configs


def restart_map():
    return OrderedDict([(cfg, v['services'])
                        for cfg, v in resource_map().iteritems()
                        if v['services']])


def services():
    """Returns a list of services associate with this charm"""
    _services = []
    for v in restart_map().values():
        _services = _services + v
    return list(set(_services))


def determine_ports():
    """Assemble a list of API ports for services we are managing"""
    ports = [config('admin-port'), config('service-port')]
    return list(set(ports))


def api_port(service):
    return API_PORTS[service]


def determine_packages():
    # currently all packages match service names
    packages = [] + BASE_PACKAGES
    for k, v in resource_map().iteritems():
        packages.extend(v['services'])

    if git_install_requested():
        packages.extend(BASE_GIT_PACKAGES)
        # don't include packages that will be installed from git
        for p in GIT_PACKAGE_BLACKLIST:
            packages.remove(p)

    return list(set(packages))


def save_script_rc():
    env_vars = {'OPENSTACK_SERVICE_KEYSTONE': 'keystone',
                'OPENSTACK_PORT_ADMIN': determine_api_port(
                    api_port('keystone-admin'), singlenode_mode=True),
                'OPENSTACK_PORT_PUBLIC': determine_api_port(
                    api_port('keystone-public'),
                    singlenode_mode=True)}
    _save_script_rc(**env_vars)


def do_openstack_upgrade(configs):
    new_src = config('openstack-origin')
    new_os_rel = get_os_codename_install_source(new_src)
    log('Performing OpenStack upgrade to %s.' % (new_os_rel))

    configure_installation_source(new_src)
    apt_update()

    dpkg_opts = [
        '--option', 'Dpkg::Options::=--force-confnew',
        '--option', 'Dpkg::Options::=--force-confdef',
    ]
    apt_upgrade(options=dpkg_opts, fatal=True, dist=True)
    apt_install(packages=determine_packages(), options=dpkg_opts, fatal=True)

    # set CONFIGS to load templates from new release and regenerate config
    configs.set_release(openstack_release=new_os_rel)
    configs.write_all()

    if is_elected_leader(CLUSTER_RES):
        if is_db_ready():
            migrate_database()
        else:
            log("Database not ready - deferring to shared-db relation",
                level=INFO)
            return


def set_db_initialised():
    for rid in relation_ids('cluster'):
        relation_set(relation_settings={'db-initialised': 'True'},
                     relation_id=rid)


def is_db_initialised():
    for rid in relation_ids('cluster'):
        units = related_units(rid) + [local_unit()]
        for unit in units:
            db_initialised = relation_get(attribute='db-initialised',
                                          unit=unit, rid=rid)
            if db_initialised:
                log("Database is initialised", level=DEBUG)
                return True

    log("Database is NOT initialised", level=DEBUG)
    return False


def migrate_database():
    """Runs keystone-manage to initialize a new database or migrate existing"""
    log('Migrating the keystone database.', level=INFO)
    service_stop('keystone')
    # NOTE(jamespage) > icehouse creates a log file as root so use
    # sudo to execute as keystone otherwise keystone won't start
    # afterwards.
    cmd = ['sudo', '-u', 'keystone', 'keystone-manage', 'db_sync']
    subprocess.check_output(cmd)
    service_start('keystone')
    time.sleep(10)
    set_db_initialised()

# OLD


def get_local_endpoint():
    """Returns the URL for the local end-point bypassing haproxy/ssl"""
    if config('prefer-ipv6'):
        ipv6_addr = get_ipv6_addr(exc_list=[config('vip')])[0]
        endpoint_url = 'http://[%s]:{}/v2.0/' % ipv6_addr
        local_endpoint = endpoint_url.format(
            determine_api_port(api_port('keystone-admin'),
                               singlenode_mode=True))
    else:
        local_endpoint = 'http://localhost:{}/v2.0/'.format(
            determine_api_port(api_port('keystone-admin'),
                               singlenode_mode=True))

    return local_endpoint


def set_admin_token(admin_token='None'):
    """Set admin token according to deployment config or use a randomly
       generated token if none is specified (default).
    """
    if admin_token != 'None':
        log('Configuring Keystone to use a pre-configured admin token.')
        token = admin_token
    else:
        log('Configuring Keystone to use a random admin token.')
        if os.path.isfile(STORED_TOKEN):
            msg = 'Loading a previously generated' \
                  ' admin token from %s' % STORED_TOKEN
            log(msg)
            with open(STORED_TOKEN, 'r') as f:
                token = f.read().strip()
        else:
            token = pwgen(length=64)
            with open(STORED_TOKEN, 'w') as out:
                out.write('%s\n' % token)
    return(token)


def get_admin_token():
    """Temporary utility to grab the admin token as configured in
       keystone.conf
    """
    with open(KEYSTONE_CONF, 'r') as f:
        for l in f.readlines():
            if l.split(' ')[0] == 'admin_token':
                try:
                    return l.split('=')[1].strip()
                except:
                    error_out('Could not parse admin_token line from %s' %
                              KEYSTONE_CONF)
    error_out('Could not find admin_token line in %s' % KEYSTONE_CONF)


def create_service_entry(service_name, service_type, service_desc, owner=None):
    """ Add a new service entry to keystone if one does not already exist """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    for service in [s._info for s in manager.api.services.list()]:
        if service['name'] == service_name:
            log("Service entry for '%s' already exists." % service_name)
            return
    manager.api.services.create(name=service_name,
                                service_type=service_type,
                                description=service_desc)
    log("Created new service entry '%s'" % service_name)


def create_endpoint_template(region, service, publicurl, adminurl,
                             internalurl):
    """ Create a new endpoint template for service if one does not already
        exist matching name *and* region """
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    service_id = manager.resolve_service_id(service)
    for ep in [e._info for e in manager.api.endpoints.list()]:
        if ep['service_id'] == service_id and ep['region'] == region:
            log("Endpoint template already exists for '%s' in '%s'"
                % (service, region))

            up_to_date = True
            for k in ['publicurl', 'adminurl', 'internalurl']:
                if ep.get(k) != locals()[k]:
                    up_to_date = False

            if up_to_date:
                return
            else:
                # delete endpoint and recreate if endpoint urls need updating.
                log("Updating endpoint template with new endpoint urls.")
                manager.api.endpoints.delete(ep['id'])

    manager.api.endpoints.create(region=region,
                                 service_id=service_id,
                                 publicurl=publicurl,
                                 adminurl=adminurl,
                                 internalurl=internalurl)
    log("Created new endpoint template for '%s' in '%s'" % (region, service))


def create_tenant(name):
    """Creates a tenant if it does not already exist"""
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    tenants = [t._info for t in manager.api.tenants.list()]
    if not tenants or name not in [t['name'] for t in tenants]:
        manager.api.tenants.create(tenant_name=name,
                                   description='Created by Juju')
        log("Created new tenant: %s" % name)
        return
    log("Tenant '%s' already exists." % name)


def create_user(name, password, tenant):
    """Creates a user if it doesn't already exist, as a member of tenant"""
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    users = [u._info for u in manager.api.users.list()]
    if not users or name not in [u['name'] for u in users]:
        tenant_id = manager.resolve_tenant_id(tenant)
        if not tenant_id:
            error_out('Could not resolve tenant_id for tenant %s' % tenant)
        manager.api.users.create(name=name,
                                 password=password,
                                 email='juju@localhost',
                                 tenant_id=tenant_id)
        log("Created new user '%s' tenant: %s" % (name, tenant_id))
        return
    log("A user named '%s' already exists" % name)


def create_role(name, user=None, tenant=None):
    """Creates a role if it doesn't already exist. grants role to user"""
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    roles = [r._info for r in manager.api.roles.list()]
    if not roles or name not in [r['name'] for r in roles]:
        manager.api.roles.create(name=name)
        log("Created new role '%s'" % name)
    else:
        log("A role named '%s' already exists" % name)

    if not user and not tenant:
        return

    # NOTE(adam_g): Keystone client requires id's for add_user_role, not names
    user_id = manager.resolve_user_id(user)
    role_id = manager.resolve_role_id(name)
    tenant_id = manager.resolve_tenant_id(tenant)

    if None in [user_id, role_id, tenant_id]:
        error_out("Could not resolve [%s, %s, %s]" %
                  (user_id, role_id, tenant_id))

    grant_role(user, name, tenant)


def grant_role(user, role, tenant):
    """Grant user and tenant a specific role"""
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    log("Granting user '%s' role '%s' on tenant '%s'" %
        (user, role, tenant))
    user_id = manager.resolve_user_id(user)
    role_id = manager.resolve_role_id(role)
    tenant_id = manager.resolve_tenant_id(tenant)

    cur_roles = manager.api.roles.roles_for_user(user_id, tenant_id)
    if not cur_roles or role_id not in [r.id for r in cur_roles]:
        manager.api.roles.add_user_role(user=user_id,
                                        role=role_id,
                                        tenant=tenant_id)
        log("Granted user '%s' role '%s' on tenant '%s'" %
            (user, role, tenant))
    else:
        log("User '%s' already has role '%s' on tenant '%s'" %
            (user, role, tenant))


def store_admin_passwd(passwd):
    with open(STORED_PASSWD, 'w+') as fd:
        fd.writelines("%s\n" % passwd)


def get_admin_passwd():
    passwd = config("admin-password")
    if passwd and passwd.lower() != "none":
        return passwd

    if is_elected_leader(CLUSTER_RES):
        if os.path.isfile(STORED_PASSWD):
            log("Loading stored passwd from %s" % STORED_PASSWD, level=INFO)
            with open(STORED_PASSWD, 'r') as fd:
                passwd = fd.readline().strip('\n')

        if not passwd:
            log("Generating new passwd for user: %s" %
                config("admin-user"))
            cmd = ['pwgen', '-c', '16', '1']
            passwd = str(subprocess.check_output(cmd)).strip()
            store_admin_passwd(passwd)

        if is_relation_made("cluster"):
            peer_store("admin_passwd", passwd)

        return passwd

    if is_relation_made("cluster"):
        passwd = peer_retrieve('admin_passwd')
        if passwd:
            store_admin_passwd(passwd)

    return passwd


def ensure_initial_admin(config):
    # Allow retry on fail since leader may not be ready yet.
    # NOTE(hopem): ks client may not be installed at module import time so we
    # use this wrapped approach instead.
    from keystoneclient.apiclient.exceptions import InternalServerError

    @retry_on_exception(3, base_delay=3, exc_type=InternalServerError)
    def _ensure_initial_admin(config):
        """Ensures the minimum admin stuff exists in whatever database we're
        using.

        This and the helper functions it calls are meant to be idempotent and
        run during install as well as during db-changed.  This will maintain
        the admin tenant, user, role, service entry and endpoint across every
        datastore we might use.

        TODO: Possibly migrate data from one backend to another after it
        changes?
        """
        create_tenant("admin")
        create_tenant(config("service-tenant"))
        # User is managed by ldap backend when using ldap identity
        if not (config('identity-backend') ==
                'ldap' and config('ldap-readonly')):
            passwd = get_admin_passwd()
            if passwd:
                create_user(config('admin-user'), passwd, tenant='admin')
                update_user_password(config('admin-user'), passwd)
                create_role(config('admin-role'), config('admin-user'),
                            'admin')
        create_service_entry("keystone", "identity",
                             "Keystone Identity Service")

        for region in config('region').split():
            create_keystone_endpoint(public_ip=resolve_address(PUBLIC),
                                     service_port=config("service-port"),
                                     internal_ip=resolve_address(INTERNAL),
                                     admin_ip=resolve_address(ADMIN),
                                     auth_port=config("admin-port"),
                                     region=region)

    return _ensure_initial_admin(config)


def endpoint_url(ip, port):
    proto = 'http'
    if https():
        proto = 'https'
    if is_ipv6(ip):
        ip = "[{}]".format(ip)
    return "%s://%s:%s/v2.0" % (proto, ip, port)


def create_keystone_endpoint(public_ip, service_port,
                             internal_ip, admin_ip, auth_port, region):
    create_endpoint_template(region, "keystone",
                             endpoint_url(public_ip, service_port),
                             endpoint_url(admin_ip, auth_port),
                             endpoint_url(internal_ip, service_port))


def update_user_password(username, password):
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    log("Updating password for user '%s'" % username)

    user_id = manager.resolve_user_id(username)
    if user_id is None:
        error_out("Could not resolve user id for '%s'" % username)

    manager.api.users.update_password(user=user_id, password=password)
    log("Successfully updated password for user '%s'" %
        username)


def load_stored_passwords(path=SERVICE_PASSWD_PATH):
    creds = {}
    if not os.path.isfile(path):
        return creds

    stored_passwd = open(path, 'r')
    for l in stored_passwd.readlines():
        user, passwd = l.strip().split(':')
        creds[user] = passwd
    return creds


def _migrate_service_passwords():
    """Migrate on-disk service passwords to peer storage"""
    if os.path.exists(SERVICE_PASSWD_PATH):
        log('Migrating on-disk stored passwords to peer storage')
        creds = load_stored_passwords()
        for k, v in creds.iteritems():
            peer_store(key="{}_passwd".format(k), value=v)
        os.unlink(SERVICE_PASSWD_PATH)


def get_service_password(service_username):
    _migrate_service_passwords()
    peer_key = "{}_passwd".format(service_username)
    passwd = peer_retrieve(peer_key)
    if passwd is None:
        passwd = pwgen(length=64)
        peer_store(key=peer_key,
                   value=passwd)
    return passwd


def ensure_permissions(path, user=None, group=None, perms=None):
    """Set chownand chmod for path

    Note that -1 for uid or gid result in no change.
    """
    if user:
        uid = pwd.getpwnam(user).pw_uid
    else:
        uid = -1

    if group:
        gid = grp.getgrnam(group).gr_gid
    else:
        gid = -1

    os.chown(path, uid, gid)

    if perms:
        os.chmod(path, perms)


def check_peer_actions():
    """Honour service action requests from sync master.

    Check for service action request flags, perform the action then delete the
    flag.
    """
    restart = relation_get(attribute='restart-services-trigger')
    if restart and os.path.isdir(SYNC_FLAGS_DIR):
        for flagfile in glob.glob(os.path.join(SYNC_FLAGS_DIR, '*')):
            flag = os.path.basename(flagfile)
            key = re.compile("^(.+)?\.(.+)?\.(.+)")
            res = re.search(key, flag)
            if res:
                source = res.group(1)
                service = res.group(2)
                action = res.group(3)
            else:
                key = re.compile("^(.+)?\.(.+)?")
                res = re.search(key, flag)
                source = res.group(1)
                action = res.group(2)

            # Don't execute actions requested by this unit.
            if local_unit().replace('.', '-') != source:
                if action == 'restart':
                    log("Running action='%s' on service '%s'" %
                        (action, service), level=DEBUG)
                    service_restart(service)
                elif action == 'start':
                    log("Running action='%s' on service '%s'" %
                        (action, service), level=DEBUG)
                    service_start(service)
                elif action == 'stop':
                    log("Running action='%s' on service '%s'" %
                        (action, service), level=DEBUG)
                    service_stop(service)
                elif action == 'update-ca-certificates':
                    log("Running %s" % (action), level=DEBUG)
                    subprocess.check_call(['update-ca-certificates'])
                else:
                    log("Unknown action flag=%s" % (flag), level=WARNING)

            try:
                os.remove(flagfile)
            except:
                pass


def create_peer_service_actions(action, services):
    """Mark remote services for action.

    Default action is restart. These action will be picked up by peer units
    e.g. we may need to restart services on peer units after certs have been
    synced.
    """
    for service in services:
        flagfile = os.path.join(SYNC_FLAGS_DIR, '%s.%s.%s' %
                                (local_unit().replace('/', '-'),
                                 service.strip(), action))
        log("Creating action %s" % (flagfile), level=DEBUG)
        write_file(flagfile, content='', owner=SSH_USER, group='keystone',
                   perms=0o644)


def create_peer_actions(actions):
    for action in actions:
        action = "%s.%s" % (local_unit().replace('/', '-'), action)
        flagfile = os.path.join(SYNC_FLAGS_DIR, action)
        log("Creating action %s" % (flagfile), level=DEBUG)
        write_file(flagfile, content='', owner=SSH_USER, group='keystone',
                   perms=0o644)


@retry_on_exception(3, base_delay=2, exc_type=subprocess.CalledProcessError)
def unison_sync(paths_to_sync):
    """Do unison sync and retry a few times if it fails since peers may not be
    ready for sync.

    Returns list of synced units or None if one or more peers was not synced.
    """
    log('Synchronizing CA (%s) to all peers.' % (', '.join(paths_to_sync)),
        level=INFO)
    keystone_gid = grp.getgrnam('keystone').gr_gid

    # NOTE(dosaboy): This will sync to all peers who have already provided
    # their ssh keys. If any existing peers have not provided their keys yet,
    # they will be silently ignored.
    unison.sync_to_peers(peer_interface='cluster', paths=paths_to_sync,
                         user=SSH_USER, verbose=True, gid=keystone_gid,
                         fatal=True)

    synced_units = peer_units()
    if len(unison.collect_authed_hosts('cluster')) != len(synced_units):
        log("Not all peer units synced due to missing public keys", level=INFO)
        return None
    else:
        return synced_units


def get_ssl_sync_request_units():
    """Get list of units that have requested to be synced.

    NOTE: this must be called from cluster relation context.
    """
    units = []
    for unit in related_units():
        settings = relation_get(unit=unit) or {}
        rkeys = settings.keys()
        key = re.compile("^ssl-sync-required-(.+)")
        for rkey in rkeys:
            res = re.search(key, rkey)
            if res:
                units.append(res.group(1))

    return units


def is_ssl_cert_master(votes=None):
    """Return True if this unit is ssl cert master."""
    master = None
    for rid in relation_ids('cluster'):
        master = relation_get(attribute='ssl-cert-master', rid=rid,
                              unit=local_unit())

    if master == local_unit():
        votes = votes or get_ssl_cert_master_votes()
        if not peer_units() or (len(votes) == 1 and master in votes):
            return True

        log("Did not get consensus from peers on who is ssl-cert-master "
            "(%s)" % (votes), level=INFO)

    return False


def is_ssl_enabled():
    if (bool_from_string(config('use-https')) or
            bool_from_string(config('https-service-endpoints'))):
        log("SSL/HTTPS is enabled", level=DEBUG)
        return True

    log("SSL/HTTPS is NOT enabled", level=DEBUG)
    return True


def get_ssl_cert_master_votes():
    """Returns a list of unique votes."""
    votes = []
    # Gather election results from peers. These will need to be consistent.
    for rid in relation_ids('cluster'):
        for unit in related_units(rid):
            m = relation_get(rid=rid, unit=unit,
                             attribute='ssl-cert-master')
            if m is not None:
                votes.append(m)

    return list(set(votes))


def ensure_ssl_cert_master():
    """Ensure that an ssl cert master has been elected.

    Normally the cluster leader will take control but we allow for this to be
    ignored since this could be called before the cluster is ready.
    """
    # Don't do anything if we are not in ssl/https mode
    if not is_ssl_enabled():
        return False

    master_override = False
    elect = is_elected_leader(CLUSTER_RES)

    # If no peers we allow this unit to elect itsef as master and do
    # sync immediately.
    if not peer_units():
        elect = True
        master_override = True

    if elect:
        votes = get_ssl_cert_master_votes()
        # We expect all peers to echo this setting
        if not votes or 'unknown' in votes:
            log("Notifying peers this unit is ssl-cert-master", level=INFO)
            for rid in relation_ids('cluster'):
                settings = {'ssl-cert-master': local_unit()}
                relation_set(relation_id=rid, relation_settings=settings)

            # Return now and wait for cluster-relation-changed (peer_echo) for
            # sync.
            return master_override
        elif not is_ssl_cert_master(votes):
            if not master_override:
                log("Conscensus not reached - current master will need to "
                    "release", level=INFO)

            return master_override

    if not is_ssl_cert_master():
        log("Not ssl cert master - skipping sync", level=INFO)
        return False

    return True


def synchronize_ca(fatal=False):
    """Broadcast service credentials to peers.

    By default a failure to sync is fatal and will result in a raised
    exception.

    This function uses a relation setting 'ssl-cert-master' to get some
    leader stickiness while synchronisation is being carried out. This ensures
    that the last host to create and broadcast cetificates has the option to
    complete actions before electing the new leader as sync master.

    Returns a dictionary of settings to be set on the cluster relation.
    """
    paths_to_sync = [SYNC_FLAGS_DIR]

    if bool_from_string(config('https-service-endpoints')):
        log("Syncing all endpoint certs since https-service-endpoints=True",
            level=DEBUG)
        paths_to_sync.append(SSL_DIR)
        paths_to_sync.append(CA_CERT_PATH)

    if bool_from_string(config('use-https')):
        log("Syncing keystone-endpoint certs since use-https=True",
            level=DEBUG)
        paths_to_sync.append(SSL_DIR)
        paths_to_sync.append(APACHE_SSL_DIR)
        paths_to_sync.append(CA_CERT_PATH)

    # Ensure unique
    paths_to_sync = list(set(paths_to_sync))

    if not paths_to_sync:
        log("Nothing to sync - skipping", level=DEBUG)
        return {}

    if not os.path.isdir(SYNC_FLAGS_DIR):
        mkdir(SYNC_FLAGS_DIR, SSH_USER, 'keystone', 0o775)

    # We need to restart peer apache services to ensure they have picked up
    # new ssl keys.
    create_peer_service_actions('restart', ['apache2'])
    create_peer_actions(['update-ca-certificates'])

    cluster_rel_settings = {}

    retries = 3
    while True:
        hash1 = hashlib.sha256()
        for path in paths_to_sync:
            update_hash_from_path(hash1, path)

        try:
            synced_units = unison_sync(paths_to_sync)
            if synced_units:
                # Format here needs to match that used when peers request sync
                synced_units = [u.replace('/', '-') for u in synced_units]
                cluster_rel_settings['ssl-synced-units'] = \
                    json.dumps(synced_units)
        except:
            if fatal:
                raise
            else:
                log("Sync failed but fatal=False", level=INFO)
                return {}

        hash2 = hashlib.sha256()
        for path in paths_to_sync:
            update_hash_from_path(hash2, path)

        # Detect whether someone else has synced to this unit while we did our
        # transfer.
        if hash1.hexdigest() != hash2.hexdigest():
            retries -= 1
            if retries > 0:
                log("SSL dir contents changed during sync - retrying unison "
                    "sync %s more times" % (retries), level=WARNING)
            else:
                log("SSL dir contents changed during sync - retries failed",
                    level=ERROR)
                return {}
        else:
            break

    hash = hash1.hexdigest()
    log("Sending restart-services-trigger=%s to all peers" % (hash),
        level=DEBUG)
    cluster_rel_settings['restart-services-trigger'] = hash

    log("Sync complete", level=DEBUG)
    return cluster_rel_settings


def clear_ssl_synced_units():
    """Clear the 'synced' units record on the cluster relation.

    If new unit sync reauests are set this will ensure that a sync occurs when
    the sync master receives the requests.
    """
    log("Clearing ssl sync units", level=DEBUG)
    for rid in relation_ids('cluster'):
        relation_set(relation_id=rid,
                     relation_settings={'ssl-synced-units': None})


def update_hash_from_path(hash, path, recurse_depth=10):
    """Recurse through path and update the provided hash for every file found.
    """
    if not recurse_depth:
        log("Max recursion depth (%s) reached for update_hash_from_path() at "
            "path='%s' - not going any deeper" % (recurse_depth, path),
            level=WARNING)
        return

    for p in glob.glob("%s/*" % path):
        if os.path.isdir(p):
            update_hash_from_path(hash, p, recurse_depth=recurse_depth - 1)
        else:
            with open(p, 'r') as fd:
                hash.update(fd.read())


def synchronize_ca_if_changed(force=False, fatal=False):
    """Decorator to perform ssl cert sync if decorated function modifies them
    in any way.

    If force is True a sync is done regardless.
    """
    def inner_synchronize_ca_if_changed1(f):
        def inner_synchronize_ca_if_changed2(*args, **kwargs):
            # Only sync master can do sync. Ensure (a) we are not nested and
            # (b) a master is elected and we are it.
            acquired = SSL_SYNC_SEMAPHORE.acquire(blocking=0)
            try:
                if not acquired:
                    log("Nested sync - ignoring", level=DEBUG)
                    return f(*args, **kwargs)

                if not ensure_ssl_cert_master():
                    log("Not leader - ignoring sync", level=DEBUG)
                    return f(*args, **kwargs)

                peer_settings = {}
                if not force:
                    ssl_dirs = [SSL_DIR, APACHE_SSL_DIR, CA_CERT_PATH]

                    hash1 = hashlib.sha256()
                    for path in ssl_dirs:
                        update_hash_from_path(hash1, path)

                    ret = f(*args, **kwargs)

                    hash2 = hashlib.sha256()
                    for path in ssl_dirs:
                        update_hash_from_path(hash2, path)

                    if hash1.hexdigest() != hash2.hexdigest():
                        log("SSL certs have changed - syncing peers",
                            level=DEBUG)
                        peer_settings = synchronize_ca(fatal=fatal)
                    else:
                        log("SSL certs have not changed - skipping sync",
                            level=DEBUG)
                else:
                    ret = f(*args, **kwargs)
                    log("Doing forced ssl cert sync", level=DEBUG)
                    peer_settings = synchronize_ca(fatal=fatal)

                # If we are the sync master but not leader, ensure we have
                # relinquished master status.
                if not is_elected_leader(CLUSTER_RES):
                    log("Re-electing ssl cert master.", level=INFO)
                    peer_settings['ssl-cert-master'] = 'unknown'

                if peer_settings:
                    for rid in relation_ids('cluster'):
                        relation_set(relation_id=rid,
                                     relation_settings=peer_settings)

                return ret
            finally:
                SSL_SYNC_SEMAPHORE.release()

        return inner_synchronize_ca_if_changed2

    return inner_synchronize_ca_if_changed1


def get_ca(user='keystone', group='keystone'):
    """Initialize a new CA object if one hasn't already been loaded.

    This will create a new CA or load an existing one.
    """
    if not ssl.CA_SINGLETON:
        if not os.path.isdir(SSL_DIR):
            os.mkdir(SSL_DIR)

        d_name = '_'.join(SSL_CA_NAME.lower().split(' '))
        ca = ssl.JujuCA(name=SSL_CA_NAME, user=user, group=group,
                        ca_dir=os.path.join(SSL_DIR,
                                            '%s_intermediate_ca' % d_name),
                        root_ca_dir=os.path.join(SSL_DIR,
                                                 '%s_root_ca' % d_name))

        # SSL_DIR is synchronized via all peers over unison+ssh, need
        # to ensure permissions.
        subprocess.check_output(['chown', '-R', '%s.%s' % (user, group),
                                 '%s' % SSL_DIR])
        subprocess.check_output(['chmod', '-R', 'g+rwx', '%s' % SSL_DIR])

        # Ensure a master is elected. This should cover the following cases:
        # * single unit == 'oldest' unit is elected as master
        # * multi unit + not clustered == 'oldest' unit is elcted as master
        # * multi unit + clustered == cluster leader is elected as master
        ensure_ssl_cert_master()

        ssl.CA_SINGLETON.append(ca)

    return ssl.CA_SINGLETON[0]


def relation_list(rid):
    cmd = [
        'relation-list',
        '-r', rid,
    ]
    result = str(subprocess.check_output(cmd)).split()
    if result == "":
        return None
    else:
        return result


def add_service_to_keystone(relation_id=None, remote_unit=None):
    import manager
    manager = manager.KeystoneManager(endpoint=get_local_endpoint(),
                                      token=get_admin_token())
    settings = relation_get(rid=relation_id, unit=remote_unit)
    # the minimum settings needed per endpoint
    single = set(['service', 'region', 'public_url', 'admin_url',
                  'internal_url'])
    https_cns = []

    if https():
        protocol = 'https'
    else:
        protocol = 'http'

    if single.issubset(settings):
        # other end of relation advertised only one endpoint
        if 'None' in settings.itervalues():
            # Some backend services advertise no endpoint but require a
            # hook execution to update auth strategy.
            relation_data = {}
            # Check if clustered and use vip + haproxy ports if so
            relation_data["auth_host"] = resolve_address(ADMIN)
            relation_data["service_host"] = resolve_address(PUBLIC)
            relation_data["auth_protocol"] = protocol
            relation_data["service_protocol"] = protocol
            relation_data["auth_port"] = config('admin-port')
            relation_data["service_port"] = config('service-port')
            relation_data["region"] = config('region')

            https_service_endpoints = config('https-service-endpoints')
            if (https_service_endpoints and
                    bool_from_string(https_service_endpoints)):
                # Pass CA cert as client will need it to
                # verify https connections
                ca = get_ca(user=SSH_USER)
                ca_bundle = ca.get_ca_bundle()
                relation_data['https_keystone'] = 'True'
                relation_data['ca_cert'] = b64encode(ca_bundle)

            # Allow the remote service to request creation of any additional
            # roles. Currently used by Horizon
            for role in get_requested_roles(settings):
                log("Creating requested role: %s" % role)
                create_role(role)

            peer_store_and_set(relation_id=relation_id,
                               **relation_data)
            return
        else:
            ensure_valid_service(settings['service'])
            add_endpoint(region=settings['region'],
                         service=settings['service'],
                         publicurl=settings['public_url'],
                         adminurl=settings['admin_url'],
                         internalurl=settings['internal_url'])

            # If an admin username prefix is provided, ensure all services use
            # it.
            service_username = settings['service']
            prefix = config('service-admin-prefix')
            if prefix:
                service_username = "%s%s" % (prefix, service_username)

            # NOTE(jamespage) internal IP for backwards compat for SSL certs
            internal_cn = urlparse.urlparse(settings['internal_url']).hostname
            https_cns.append(internal_cn)
            public_cn = urlparse.urlparse(settings['public_url']).hostname
            https_cns.append(public_cn)
            https_cns.append(urlparse.urlparse(settings['admin_url']).hostname)
    else:
        # assemble multiple endpoints from relation data. service name
        # should be prepended to setting name, ie:
        #  realtion-set ec2_service=$foo ec2_region=$foo ec2_public_url=$foo
        #  relation-set nova_service=$foo nova_region=$foo nova_public_url=$foo
        # Results in a dict that looks like:
        # { 'ec2': {
        #       'service': $foo
        #       'region': $foo
        #       'public_url': $foo
        #   }
        #   'nova': {
        #       'service': $foo
        #       'region': $foo
        #       'public_url': $foo
        #   }
        # }
        endpoints = {}
        for k, v in settings.iteritems():
            ep = k.split('_')[0]
            x = '_'.join(k.split('_')[1:])
            if ep not in endpoints:
                endpoints[ep] = {}
            endpoints[ep][x] = v

        services = []
        https_cn = None
        for ep in endpoints:
            # weed out any unrelated relation stuff Juju might have added
            # by ensuring each possible endpiont has appropriate fields
            #  ['service', 'region', 'public_url', 'admin_url', 'internal_url']
            if single.issubset(endpoints[ep]):
                ep = endpoints[ep]
                ensure_valid_service(ep['service'])
                add_endpoint(region=ep['region'], service=ep['service'],
                             publicurl=ep['public_url'],
                             adminurl=ep['admin_url'],
                             internalurl=ep['internal_url'])
                services.append(ep['service'])
                # NOTE(jamespage) internal IP for backwards compat for
                # SSL certs
                internal_cn = urlparse.urlparse(ep['internal_url']).hostname
                https_cns.append(internal_cn)
                https_cns.append(urlparse.urlparse(ep['public_url']).hostname)
                https_cns.append(urlparse.urlparse(ep['admin_url']).hostname)

        service_username = '_'.join(services)

        # If an admin username prefix is provided, ensure all services use it.
        prefix = config('service-admin-prefix')
        if prefix:
            service_username = "%s%s" % (prefix, service_username)

    if 'None' in settings.itervalues():
        return

    if not service_username:
        return

    token = get_admin_token()
    log("Creating service credentials for '%s'" % service_username)

    service_password = get_service_password(service_username)
    create_user(service_username, service_password, config('service-tenant'))
    grant_role(service_username, config('admin-role'),
               config('service-tenant'))

    # Allow the remote service to request creation of any additional roles.
    # Currently used by Swift and Ceilometer.
    for role in get_requested_roles(settings):
        log("Creating requested role: %s" % role)
        create_role(role, service_username, config('service-tenant'))

    # As of https://review.openstack.org/#change,4675, all nodes hosting
    # an endpoint(s) needs a service username and password assigned to
    # the service tenant and granted admin role.
    # note: config('service-tenant') is created in utils.ensure_initial_admin()
    # we return a token, information about our API endpoints, and the generated
    # service credentials
    service_tenant = config('service-tenant')

    # NOTE(dosaboy): we use __null__ to represent settings that are to be
    # routed to relations via the cluster relation and set to None.
    relation_data = {
        "admin_token": token,
        "service_host": resolve_address(PUBLIC),
        "service_port": config("service-port"),
        "auth_host": resolve_address(ADMIN),
        "auth_port": config("admin-port"),
        "service_username": service_username,
        "service_password": service_password,
        "service_tenant": service_tenant,
        "service_tenant_id": manager.resolve_tenant_id(service_tenant),
        "https_keystone": '__null__',
        "ssl_cert": '__null__',
        "ssl_key": '__null__',
        "ca_cert": '__null__',
        "auth_protocol": protocol,
        "service_protocol": protocol,
    }

    # generate or get a new cert/key for service if set to manage certs.
    https_service_endpoints = config('https-service-endpoints')
    if https_service_endpoints and bool_from_string(https_service_endpoints):
        ca = get_ca(user=SSH_USER)
        # NOTE(jamespage) may have multiple cns to deal with to iterate
        https_cns = set(https_cns)
        for https_cn in https_cns:
            cert, key = ca.get_cert_and_key(common_name=https_cn)
            relation_data['ssl_cert_{}'.format(https_cn)] = b64encode(cert)
            relation_data['ssl_key_{}'.format(https_cn)] = b64encode(key)

        # NOTE(jamespage) for backwards compatibility
        cert, key = ca.get_cert_and_key(common_name=internal_cn)
        relation_data['ssl_cert'] = b64encode(cert)
        relation_data['ssl_key'] = b64encode(key)
        ca_bundle = ca.get_ca_bundle()
        relation_data['ca_cert'] = b64encode(ca_bundle)
        relation_data['https_keystone'] = 'True'

    # NOTE(dosaboy): '__null__' settings are for peer relation only so that
    # settings can flushed so we filter them out for non-peer relation.
    filtered = filter_null(relation_data)
    relation_set(relation_id=relation_id, **filtered)
    for rid in relation_ids('cluster'):
        relation_set(relation_id=rid, **relation_data)


def ensure_valid_service(service):
    if service not in valid_services.keys():
        log("Invalid service requested: '%s'" % service)
        relation_set(admin_token=-1)
        return


def add_endpoint(region, service, publicurl, adminurl, internalurl):
    desc = valid_services[service]["desc"]
    service_type = valid_services[service]["type"]
    create_service_entry(service, service_type, desc)
    create_endpoint_template(region=region, service=service,
                             publicurl=publicurl,
                             adminurl=adminurl,
                             internalurl=internalurl)


def get_requested_roles(settings):
    """Retrieve any valid requested_roles from dict settings"""
    if ('requested_roles' in settings and
            settings['requested_roles'] not in ['None', None]):
        return settings['requested_roles'].split(',')
    else:
        return []


def setup_ipv6():
    """Check ipv6-mode validity and setup dependencies"""
    ubuntu_rel = lsb_release()['DISTRIB_CODENAME'].lower()
    if ubuntu_rel < "trusty":
        raise Exception("IPv6 is not supported in the charms for Ubuntu "
                        "versions less than Trusty 14.04")

    # NOTE(xianghui): Need to install haproxy(1.5.3) from trusty-backports
    # to support ipv6 address, so check is required to make sure not
    # breaking other versions, IPv6 only support for >= Trusty
    if ubuntu_rel == 'trusty':
        add_source('deb http://archive.ubuntu.com/ubuntu trusty-backports'
                   ' main')
        apt_update()
        apt_install('haproxy/trusty-backports', fatal=True)


def send_notifications(data, force=False):
    """Send notifications to all units listening on the identity-notifications
    interface.

    Units are expected to ignore notifications that they don't expect.

    NOTE: settings that are not required/inuse must always be set to None
          so that they are removed from the relation.

    :param data: Dict of key=value to use as trigger for notification. If the
                 last broadcast is unchanged by the addition of this data, the
                 notification will not be sent.
    :param force: Determines whether a trigger value is set to ensure the
                  remote hook is fired.
    """
    if not data or not is_elected_leader(CLUSTER_RES):
        log("Not sending notifications (no data or not leader)", level=INFO)
        return

    rel_ids = relation_ids('identity-notifications')
    if not rel_ids:
        log("No relations on identity-notifications - skipping broadcast",
            level=INFO)
        return

    keys = []
    diff = False

    # Get all settings previously sent
    for rid in rel_ids:
        rs = relation_get(unit=local_unit(), rid=rid)
        if rs:
            keys += rs.keys()

        # Don't bother checking if we have already identified a diff
        if diff:
            continue

        # Work out if this notification changes anything
        for k, v in data.iteritems():
            if rs.get(k, None) != v:
                diff = True
                break

    if not diff:
        log("Notifications unchanged by new values so skipping broadcast",
            level=INFO)
        return

    # Set all to None
    _notifications = {k: None for k in set(keys)}

    # Set new values
    for k, v in data.iteritems():
        _notifications[k] = v

    if force:
        _notifications['trigger'] = str(uuid.uuid4())

    # Broadcast
    log("Sending identity-service notifications (trigger=%s)" % (force),
        level=DEBUG)
    for rid in rel_ids:
        relation_set(relation_id=rid, relation_settings=_notifications)


def is_db_ready(use_current_context=False, db_rel=None):
    """Database relations are expected to provide a list of 'allowed' units to
    confirm that the database is ready for use by those units.

    If db relation has provided this information and local unit is a member,
    returns True otherwise False.
    """
    key = 'allowed_units'
    db_rels = ['shared-db', 'pgsql-db']
    if db_rel:
        db_rels = [db_rel]

    rel_has_units = False

    if use_current_context:
        if not any([relation_id() in relation_ids(r) for r in db_rels]):
            raise Exception("use_current_context=True but not in one of %s "
                            "rel hook contexts (currently in %s)." %
                            (', '.join(db_rels), relation_id()))

        allowed_units = relation_get(attribute=key)
        if allowed_units and local_unit() in allowed_units.split():
            return True
    else:
        for rel in db_rels:
            for rid in relation_ids(rel):
                for unit in related_units(rid):
                    allowed_units = relation_get(rid=rid, unit=unit,
                                                 attribute=key)
                    if allowed_units and local_unit() in allowed_units.split():
                        return True

                    rel_has_units = True

    # If neither relation has units then we are probably in sqlite mode so
    # return True.
    return not rel_has_units


def git_install(projects):
    """Perform setup, and install git repos specified in yaml parameter."""
    if git_install_requested():
        git_pre_install()
        git_clone_and_install(yaml.load(projects), core_project='keystone')
        git_post_install()


def git_pre_install():
    """Perform pre keystone installation setup."""
    dirs = [
        '/var/lib/keystone',
        '/var/lib/keystone/cache',
        '/var/log/keystone',
        '/etc/keystone',
    ]

    logs = [
        '/var/log/keystone/keystone.log',
    ]

    adduser('keystone', shell='/bin/bash', system_user=True)
    add_group('keystone', system_group=True)
    add_user_to_group('keystone', 'keystone')

    for d in dirs:
        mkdir(d, owner='keystone', group='keystone', perms=0700, force=False)

    for l in logs:
        write_file(l, '', owner='keystone', group='keystone', perms=0600)


def git_post_install():
    """Perform post keystone installation setup."""
    src_etc = os.path.join(charm_dir(), '/mnt/openstack-git/keystone.git/etc/')
    configs = {
        'keystone': {
            'src': os.path.join(src_etc, 'keystone.conf.sample'),
            'dest': '/etc/keystone/keystone.conf',
        },
        'policy': {
            'src': os.path.join(src_etc, 'policy.json'),
            'dest': '/etc/keystone/policy.json',
        },
        'keystone-paste': {
            'src': os.path.join(src_etc, 'keystone-paste.ini'),
            'dest': '/etc/keystone/keystone-paste.ini',
        },
    }

    for conf, files in configs.iteritems():
        shutil.copyfile(files['src'], files['dest'])

    render('logging.conf', '/etc/keystone/logging.conf', {}, perms=0o644)

    keystone_context = {
        'service_description': 'Keystone API server',
        'service_name': 'Keystone',
        'user_name': 'keystone',
        'start_dir': '/var/lib/keystone',
        'process_name': 'keystone',
        'executable_name': '/usr/local/bin/keystone-all',
    }

    render('upstart/keystone.upstart', '/etc/init/keystone.conf',
           keystone_context, perms=0o644)

    service_start('keystone')
