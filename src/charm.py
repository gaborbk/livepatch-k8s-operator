#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

# Learn more at: https://juju.is/docs/sdk

"""Livepatch k8s charm."""
from base64 import b64decode
from typing import Dict, Optional
from urllib.parse import ParseResult, urlunparse

import pgsql
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.grafana_k8s.v0.grafana_dashboard import GrafanaDashboardProvider
from charms.loki_k8s.v0.loki_push_api import LogProxyConsumer
from charms.nginx_ingress_integrator.v0.nginx_route import require_nginx_route
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointProvider
from ops import pebble
from ops.charm import ActionEvent, CharmBase, HookEvent, RelationChangedEvent, RelationDepartedEvent
from ops.main import main
from ops.model import ActiveStatus, BlockedStatus, Container, ModelError, RelationDataContent, WaitingStatus

import utils
from constants import LOGGER, SCHEMA_UPGRADE_CONTAINER, WORKLOAD_CONTAINER
from state import State

SERVER_PORT = 8080
DATABASE_NAME = "livepatch-server"
LOG_FILE = "/var/log/livepatch"
LOGROTATE_CONFIG_PATH = "/etc/logrotate.d/livepatch"
LIVEPATCH_SERVICE_NAME = "livepatch"

DATABASE_RELATION = "database"
DATABASE_RELATION_LEGACY = "database-legacy"
PRO_AIRGAPPED_SERVER_RELATION = "pro-airgapped-server"
CVE_CATALOG_RELATION = "cve-catalog"

REQUIRED_SETTINGS = {
    "server.url-template": "✘ server.url-template config not set",
}
ON_PREM_REQUIRED_SETTINGS: Dict[str, str] = {}
# Template for storing trusted certificate in a file.
TRUSTED_CA_FILENAME = "/usr/local/share/ca-certificates/trusted-contracts.ca.crt"


class DeferError(Exception):
    """An exception that indicates the event should be deferred."""


class LivepatchCharm(CharmBase):
    """The livepatch k8s charm."""

    def __init__(self, *args):
        """Init function."""
        super().__init__(*args)

        self._state = State(self.app, lambda: self.model.get_relation("livepatch"))

        self.framework.observe(self.on.livepatch_relation_changed, self.on_peer_relation_changed)
        self.framework.observe(self.on.config_changed, self.on_config_changed)
        self.framework.observe(self.on.update_status, self.on_update_status)
        self.framework.observe(self.on.leader_elected, self.on_leader_elected)
        self.framework.observe(self.on.livepatch_pebble_ready, self.on_pebble_ready)
        self.framework.observe(self.on.start, self.on_start)
        self.framework.observe(self.on.stop, self.on_stop)

        self.framework.observe(self.on.restart_action, self.restart_action)
        self.framework.observe(self.on.schema_upgrade_action, self.schema_upgrade_action)
        self.framework.observe(self.on.schema_version_action, self.schema_version_check_action)

        self.framework.observe(self.on.get_resource_token_action, self.get_resource_token_action)

        # Legacy database support
        self.legacy_db = pgsql.PostgreSQLClient(self, DATABASE_RELATION_LEGACY)
        self.framework.observe(
            self.legacy_db.on.database_relation_joined,
            self._on_legacy_db_relation_joined,
        )
        self.framework.observe(self.legacy_db.on.master_changed, self._on_legacy_db_master_changed)
        self.framework.observe(self.legacy_db.on.standby_changed, self._on_legacy_db_standby_changed)

        # Database
        self.database = DatabaseRequires(
            self,
            relation_name=DATABASE_RELATION,
            database_name=DATABASE_NAME,
        )
        self.framework.observe(self.database.on.database_created, self._on_database_event)
        self.framework.observe(
            self.database.on.endpoints_changed,
            self._on_database_event,
        )

        # Air-gapped pro/contracts
        self.framework.observe(
            self.on.pro_airgapped_server_relation_changed, self._on_pro_airgapped_server_relation_changed
        )
        self.framework.observe(
            self.on.pro_airgapped_server_relation_departed, self._on_pro_airgapped_server_relation_departed
        )

        # Livepatch CVE service
        self.framework.observe(self.on.cve_catalog_relation_changed, self._on_cve_catalog_relation_changed)
        self.framework.observe(self.on.cve_catalog_relation_broken, self._on_cve_catalog_relation_broken)

        # Ingress (nginx-routes interface)
        require_nginx_route(
            charm=self,
            service_hostname=self.app.name,
            service_name=self.app.name,
            service_port=8080,
        )

        # Loki log-proxy relation
        self.log_proxy = LogProxyConsumer(
            self,
            log_files=[LOG_FILE],
            relation_name="log-proxy",
            promtail_resource_name="promtail-bin",
            container_name=WORKLOAD_CONTAINER,
        )

        # Prometheus metrics endpoint relation
        self.metrics_endpoint = MetricsEndpointProvider(
            self,
            jobs=[{"static_configs": [{"targets": [f"*:{SERVER_PORT}"]}]}],
            refresh_event=self.on.config_changed,
            relation_name="metrics-endpoint",
        )

        # Grafana dashboard relation
        self._grafana_dashboards = GrafanaDashboardProvider(self, relation_name="grafana-dashboard")

    def on_peer_relation_changed(self, event):
        """
        On peer relation changed hook.

        This hook is for the non-leader units to get notified when the state
        changes. On the leader unit this hook should be ignored to avoid
        repetitive workload restarts while handling relations. This also means,
        on the leader unit, whenever the state changes, the update workload
        method should be called manually.
        """
        if self.unit.is_leader():
            return
        if not self._state.is_ready():
            LOGGER.warning("State is not ready")
            return
        self._update_workload_container_config(event)

    def on_config_changed(self, event):
        """On config changed hook, which runs first."""
        self._update_workload_container_config(event)

    def on_start(self, event):
        """On start hook, which runs after the on-config-changed hook."""
        self._update_workload_container_config(event)

    # Runs third and on any container restarts & does not guarantee the container is "still up"
    # Runs additionally when; a new unit is created, and upgrade-charm has been run
    def on_pebble_ready(self, event):
        """On pebble ready hook, which runs after the on-start hook."""
        self._update_workload_container_config(event)

    def on_update_status(self, event):
        """On update status."""
        workload = self.unit.get_container(WORKLOAD_CONTAINER)
        self._ready(workload)

    # Runs AFTER peer-relation-created
    # When a leader loses leadership it only sees the leader-settings-changed
    # As such you will only receive this even if YOU ARE the CURRENT leader (so no need to check)
    def on_leader_elected(self, event):
        """Run after the leader is elected."""
        self._update_workload_container_config(event)

    def on_stop(self, _):
        """On stop hook."""
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect():
            try:
                service = container.get_service(LIVEPATCH_SERVICE_NAME)
            except ModelError:
                LOGGER.warning("service not found, nothing to stop")
                return
            if service.is_running():
                container.stop(LIVEPATCH_SERVICE_NAME)
        self.unit.status = WaitingStatus("service stopped")

    def handle_schema_upgrade(self):
        """Check if a schema upgrade is required, and perform it."""
        dsn = self._state.dsn
        if not dsn:
            LOGGER.info("waiting for PG connection string")
            self.unit.status = BlockedStatus("Waiting for postgres relation to be established.")
            raise DeferError()

        schema_container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not schema_container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            self.unit.status = WaitingStatus("Waiting to connect - schema container.")
            raise DeferError

        upgrade_required = False
        try:
            upgrade_required = self.migration_is_required(schema_container, dsn)
        except Exception as e:
            LOGGER.error(f"Failed to determine if schema upgrade required: {e}")

        if upgrade_required:
            self.schema_upgrade(schema_container, dsn)

    def get_env_vars(self) -> dict:
        """Map config to env vars and return a processed dict."""
        env_vars = utils.map_config_to_env_vars(self)

        env_vars["LIVEPATCH_CONFIG_LOCATION"] = "/etc/livepatch.yaml"

        # Applying `pro-airgapped-server` integration, if any.
        airgapped_pro_address = self._get_available_pro_airgapped_server_address()
        if airgapped_pro_address:
            env_vars["LP_CONTRACTS_ENABLED"] = True
            env_vars["LP_CONTRACTS_URL"] = airgapped_pro_address
            # if sync-token is not provided we disable the syncing in airgapped env.
            # the sync could be enabled when chaining multiple machines in an airgapped env.
            if not self.config.get("patch-sync.token"):
                env_vars["LP_PATCH_SYNC_ENABLED"] = False
        else:
            if not self.config.get("patch-sync.token"):
                env_vars["LP_PATCH_SYNC_TOKEN"] = self._state.resource_token
            if self.config.get("patch-sync.enabled") is True:
                # TODO: Find a better way to identify a on-prem syncing instance.
                env_vars["LP_PATCH_SYNC_ID"] = self.model.uuid

        cve_service_address = self._get_available_cve_service()
        if cve_service_address and self.unit.is_leader():
            # Note that other env vars are already set from the configuration.
            env_vars["LP_CVE_SYNC_ENABLED"] = True
            env_vars["LP_CVE_SYNC_SOURCE_URL"] = cve_service_address

        # Some extra config and checks
        env_vars["LP_DATABASE_CONNECTION_STRING"] = self._state.dsn
        env_vars["LP_SERVER_SERVER_ADDRESS"] = f":{SERVER_PORT}"

        if self.config.get("patch-storage.type") == "postgres":
            postgres_patch_storage_dsn = (
                self.config.get("patch-storage.postgres-connection-string", "") or self._state.dsn
            )
            env_vars["LP_PATCH_STORAGE_POSTGRES_CONNECTION_STRING"] = postgres_patch_storage_dsn

        # remove empty environment values
        env_vars = {key: value for key, value in env_vars.items() if value != "" and value is not None}

        return env_vars

    def _update_workload_container_config(self, event: Optional[HookEvent]):
        """
        Update workload with all available configuration data.

        Note that given event should be deferrable. For example, action events
        (of type ActionEvent), will raise exception if their `defer` method is
        invoked. So, the caller of this method should pass event as None if it's
        not a deferrable event.
        """
        if not self._state.is_ready():
            self._defer(event)
            LOGGER.warning("State is not ready")
            return

        workload_container = self.unit.get_container(WORKLOAD_CONTAINER)
        if not workload_container.can_connect():
            LOGGER.info("workload container not ready - deferring")
            self.unit.status = WaitingStatus("Waiting to connect - workload container")
            self._defer(event)
            return

        # Quickly update logrotates config each workload update
        self._push_to_workload(LOGROTATE_CONFIG_PATH, self._get_logrotate_config(), event)

        try:
            self.handle_schema_upgrade()
        except DeferError:
            self._defer(event)
            return

        # This token comes from an action rather than config so we check for it specifically.
        if not self.config.get("server.is-hosted"):
            is_airgapped = self._get_available_pro_airgapped_server_address() is not None
            # blocked status if not airgapped, no resource token in the state and no sync token in the config
            if not is_airgapped and not self._state.resource_token and not self.config.get("patch-sync.token"):
                error_msg = "✘ patch-sync token not set, run get-resource-token action"
                self.unit.status = BlockedStatus(error_msg)
                LOGGER.warning(error_msg)
                return

        # Then check for required config values.
        required_settings = REQUIRED_SETTINGS.copy()
        if self.config.get("server.is-hosted"):
            required_settings.update(ON_PREM_REQUIRED_SETTINGS)

        for setting, error_msg in required_settings.items():
            if not self.config.get(setting):
                self.unit.status = BlockedStatus(error_msg)
                LOGGER.warning(error_msg)
                return

        update_config_environment_layer = {
            "services": {
                LIVEPATCH_SERVICE_NAME: {
                    "summary": "Livepatch Service",
                    "description": "Pebble config layer for livepatch",
                    "override": "merge",
                    "startup": "disabled",
                    "command": "sh -c '/usr/local/bin/livepatch-server | tee /var/log/livepatch'",
                    "environment": self.get_env_vars(),
                },
            },
            "checks": {
                "livepatch-check": {
                    "override": "replace",
                    "period": "1m",
                    "http": {"url": f"http://localhost:{SERVER_PORT}/debug/info"},
                }
            },
        }
        layer_label = "livepatch"
        self._update_trusted_ca_certs(workload_container)
        workload_container.add_layer(layer_label, update_config_environment_layer, combine=True)
        self._start_or_restart_service(workload_container)

    def _start_or_restart_service(self, workload_container):
        if self._ready(workload_container):
            if workload_container.get_service(LIVEPATCH_SERVICE_NAME).is_running():
                workload_container.restart(LIVEPATCH_SERVICE_NAME)
            else:
                LOGGER.info("Starting Livepatch services")
                workload_container.start(LIVEPATCH_SERVICE_NAME)
        else:
            self.unit.status = WaitingStatus("Service is not ready")
            return

        self.unit.status = ActiveStatus()

    def _ready(self, workload_container):
        if workload_container.can_connect():
            plan = workload_container.get_plan()
            if plan.services.get(LIVEPATCH_SERVICE_NAME) is None:
                LOGGER.info("livepatch service is not ready yet")
                return False
            if workload_container.get_service(LIVEPATCH_SERVICE_NAME).is_running():
                self.unit.status = ActiveStatus()
            return True

        LOGGER.error("cannot connect to workload container")
        return False

    # Legacy database relation

    def _on_legacy_db_relation_joined(self, event: pgsql.DatabaseRelationJoinedEvent) -> None:
        """
        Handle determining if the database (on legacy database relation) has finished setup.

        once setup is complete a primary/standby may join / change in consequent events.
        """
        if not self._state.is_ready():
            event.defer()
            LOGGER.warning("State is not ready")
            return

        LOGGER.info("(postgresql, legacy database relation) RELATION_JOINED event fired.")

        LOGGER.warning(
            f"`{DATABASE_RELATION_LEGACY}` is a legacy relation; try integrating with `{DATABASE_RELATION}` instead."
        )

        if self.model.unit.is_leader():
            if self._is_database_relation_activated():
                LOGGER.error(f"The `{DATABASE_RELATION}` relation is already integrated.")
                raise RuntimeError(
                    "Integration with both database relations is not allowed; "
                    f"`{DATABASE_RELATION}` is already activated."
                )
            event.database = DATABASE_NAME
        elif event.database != DATABASE_NAME:
            event.defer()

    def _on_legacy_db_master_changed(self, event: pgsql.MasterChangedEvent) -> None:
        """
        Handle primary units of postgres joining / changing (for the legacy database relation).

        The internal snap configuration is updated to reflect this.
        """
        if not self._state.is_ready():
            event.defer()
            LOGGER.warning("State is not ready")
            return

        LOGGER.info("(postgresql, legacy database relation) MASTER_CHANGED event fired.")

        if event.database != DATABASE_NAME:
            LOGGER.debug("(legacy database relation) Database setup not complete yet, returning.")
            return

        if self.model.unit.is_leader():
            self.set_status_and_log(
                "(legacy database relation) Updating application database connection...", WaitingStatus
            )
            # wokeignore:rule=master
            if event.master is not None:
                # Note (babakks): The split is mainly to drop query parameters that may cause further database
                # connection errors. For example, there's this query parameters, named `fallback_application_name`,
                # which causes the schema upgrade command to return `unrecognized configuration parameter
                # "fallback_application_name" (SQLSTATE 42704)`.
                # wokeignore:rule=master
                db_uri = event.master.uri.split("?", 1)[0]
                self._state.dsn = db_uri

        self._update_workload_container_config(event)

    def _on_legacy_db_standby_changed(self, event: pgsql.StandbyChangedEvent):
        LOGGER.info("(postgresql, legacy database relation) STANDBY_CHANGED event fired.")
        # NOTE NOTE NOTE
        # This should be used for non-primary on-prem instances when configuring
        # additional livepatch instances, enabling us to read from standbys
        if event.database != DATABASE_NAME:
            # Leader has not yet set requirements. Wait until next event,
            # or risk connecting to an incorrect database.
            return
        # If read only replicas are desired, these urls should be added to
        # the peer relation e.g. peer = `[c.uri for c in event.standbys]`
        return

    # Database

    def _is_legacy_database_relation_activated(self) -> bool:
        return len(self.model.relations[DATABASE_RELATION_LEGACY]) > 0

    def _is_database_relation_activated(self) -> bool:
        return len(self.model.relations[DATABASE_RELATION]) > 0

    def _on_database_event(self, event) -> None:
        """Database event handler."""
        if not self.model.unit.is_leader():
            return

        LOGGER.info("(postgresql) RELATION_JOINED event fired.")

        if not self._state.is_ready():
            event.defer()
            LOGGER.warning("State is not ready")
            return

        if self._is_legacy_database_relation_activated():
            LOGGER.error(f"The `{DATABASE_RELATION_LEGACY}` relation is already integrated.")
            raise RuntimeError(
                "Integration with both database relations is not allowed; "
                f"`{DATABASE_RELATION_LEGACY}` is already activated."
            )

        if event.username is None or event.password is None:
            event.defer()
            LOGGER.info(
                "(postgresql) Relation data is not complete (missing `username` or `password` field); "
                "deferring the event."
            )
            return

        # get the first endpoint from a comma separate list
        ep = event.endpoints.split(",", 1)[0]
        # compose the db connection string
        uri = f"postgresql://{event.username}:{event.password}@{ep}/{DATABASE_NAME}"

        LOGGER.info(f"received database uri: {uri}")

        # record the connection string
        self._state.dsn = uri

        self._update_workload_container_config(event)

    def _on_pro_airgapped_server_relation_changed(self, event: RelationChangedEvent):
        """Handle pro-airgapped-server relation-changed event."""
        self._update_workload_container_config(event)

    def _on_pro_airgapped_server_relation_departed(self, event: RelationDepartedEvent):
        """Handle pro-airgapped-server relation-departed event."""
        self._update_workload_container_config(event)

    def _get_available_pro_airgapped_server_address(self) -> Optional[str]:
        """
        Return the pro-airgapped-server address, if any, taken from related unit databags.

        The returned value will be the same for all units. This is achieved by iterating over
        a sorted list of available units.
        """
        pro_relations = self.model.relations.get(PRO_AIRGAPPED_SERVER_RELATION, None)
        if not pro_relations or len(pro_relations) == 0:
            return None
        relation = pro_relations[0]
        sorted_units = sorted(relation.units, key=lambda unit: unit.name)
        for unit in sorted_units:
            data = relation.data.get(unit, None)
            if not data:
                continue
            address = self._extract_pro_airgapped_server_address(data)
            if address:
                return address
        return None

    def _extract_pro_airgapped_server_address(self, data: RelationDataContent) -> Optional[str]:
        """
        Extract pro-airgapped-server address from given unit databag.

        The method returns None, if data structure is not valid.
        """
        hostname = data.get("hostname")
        if not hostname:
            LOGGER.error("empty 'hostname' value in pro-airgapped relation data")
            return None

        scheme = data.get("scheme") or "http"
        port = data.get("port")
        netloc = hostname + (f":{port}" if port else "")
        return urlunparse(ParseResult(scheme, netloc, "", "", "", ""))

    def _on_cve_catalog_relation_changed(self, event: RelationChangedEvent):
        """Handle cve-catalog relation-changed event."""
        self._update_workload_container_config(event)

    def _on_cve_catalog_relation_broken(self, event: RelationDepartedEvent):
        """Handle cve-catalog relation-broken event."""
        self._update_workload_container_config(event)

    def _get_available_cve_service(self) -> Optional[str]:
        """Return the Livepatch CVE service address, if any, taken from related app/unit."""
        relation = self.model.get_relation(CVE_CATALOG_RELATION)
        if not relation:
            return None

        address = relation.data.get(relation.app).get("url", "")
        return address if address else None

    # Actions
    def restart_action(self, event):
        """Restart the workload container."""
        container = self.unit.get_container(WORKLOAD_CONTAINER)

        if container.can_connect():
            service = None
            try:
                service = container.get_service(LIVEPATCH_SERVICE_NAME)
            except ModelError:
                pass
            if service and service.is_running():
                container.stop(LIVEPATCH_SERVICE_NAME)

        # Action events are not deferrable, so we should pass event as None.
        self._update_workload_container_config(None)

    def schema_upgrade_action(self, event: ActionEvent):
        """Run the schema upgrade action."""
        if not self._state.is_ready():
            # Note that action events are not deferrable, so we should just return.
            LOGGER.warning("State is not ready")
            return

        db_uri = self._state.dsn
        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not db_uri:
            LOGGER.error("DB connection string not set")
            event.fail("schema migration failed: database connection not set/ready")
            return
        if not container.can_connect():
            LOGGER.error("Cannot connect to the schema upgrade container")
            event.fail("schema migration failed: cannot connect to schema upgrade container")
            return

        try:
            self.schema_upgrade(container, db_uri)
        except Exception as e:
            event.fail(f"schema migration failed: {e}")

    def schema_upgrade(self, container, conn_str):
        """
        Perform a schema upgrade on the configurable database.

        Raise an exception if there is a failure to prevent further charm.
        hook from firing and prevent more non-leader units from upgrading.
        """
        LOGGER.info("Attempting schema upgrade")
        self.unit.status = WaitingStatus("pg connection successful, attempting upgrade")
        if not container.exists("/usr/local/bin/livepatch-schema-tool"):
            LOGGER.error("livepatch-schema-tool not found in the schema upgrade container")
            raise FileNotFoundError("schema tool not found")

        process = None
        try:
            process = container.exec(
                command=[
                    "/usr/local/bin/livepatch-schema-tool",
                    "upgrade",
                    "/etc/livepatch/schema-upgrades",
                    "--db",
                    conn_str,
                ],
            )
        except pebble.APIError as e:
            LOGGER.error(e)
            LOGGER.error("Schema migration failed")
            raise e

        try:
            stdout, _ = process.wait_output()
            LOGGER.info(stdout)
            self.unit.status = WaitingStatus("Schema migration done")
        except pebble.ExecError as e:
            LOGGER.error(e)
            LOGGER.error("Exited with code %d. Stderr:", e.exit_code)
            for line in e.stderr.splitlines():
                LOGGER.error("    %s", line)
            LOGGER.error("Schema migration failed - executing migration failed")
            raise e

    def schema_version_check_action(self, event: ActionEvent):
        """Check schema version action."""
        if not self._state.is_ready():
            # Note that action events are not deferrable, so we should just return.
            LOGGER.warning("State is not ready")
            return

        db_uri = self._state.dsn
        container = self.unit.get_container(SCHEMA_UPGRADE_CONTAINER)
        if not container.can_connect():
            LOGGER.error("cannot connect to the schema update container")
            return

        try:
            migration_required = self.migration_is_required(container, db_uri)
            event.set_results({"migration-required": migration_required})
        except Exception as e:
            event.fail(f"schema version check failed: {e}")

    def migration_is_required(self, container, conn_str: str) -> bool:
        """Run a schema version check against the database."""
        if not container.exists("/usr/local/bin/livepatch-schema-tool"):
            LOGGER.error("livepatch-schema-tool not found in the schema upgrade container")
            raise FileNotFoundError("Failed to find schema tool")

        if not conn_str:
            LOGGER.error("Database connection string not found")
            raise ValueError("Database connection string is None")

        process = None
        try:
            process = container.exec(
                command=[
                    "/usr/local/bin/livepatch-schema-tool",
                    "check",
                    "/etc/livepatch/schema-upgrades",
                    "--db",
                    conn_str,
                ],
            )
        except pebble.APIError as e:
            LOGGER.error(e)
            raise e

        stdout = None
        try:
            stdout, _ = process.wait_output()
            LOGGER.info("Schema is up to date.")
            LOGGER.info(stdout)
            return False
        except pebble.ExecError as e:
            LOGGER.info(e.stderr)
            if e.exit_code == 2:
                # If command has a non-zero exit code then migrations are pending.
                LOGGER.info("Migrations pending")
                return True
            raise e

    def get_resource_token_action(self, event: ActionEvent):
        """Retrieve the livepatch resource token from ua-contracts."""
        if not self.unit.is_leader():
            LOGGER.error("cannot fetch the resource token: unit is not the leader")
            event.set_results({"error": "cannot fetch the resource token: unit is not the leader"})
            return

        if not self._state.is_ready():
            LOGGER.error("cannot fetch the resource token: peer relation not ready")
            event.set_results({"error": "cannot fetch the resource token: peer relation not ready"})
            return

        if self.config.get("patch-sync.token"):
            LOGGER.error("patch-sync.token is already set. It should be unset before setting a resource token")
            event.set_results(
                {"error": "patch-sync.token is already set. It should be unset before setting a resource token"}
            )
            return

        # If there already is an integration with the `pro-airgapped-server`
        # the user shouldn't be able to run this action, unless they remove the
        # relation.
        if self._get_available_pro_airgapped_server_address():
            LOGGER.error(
                "already integrated with `pro-airgapped-server`. The relation should be removed before setting a resource token"
            )
            event.set_results(
                {
                    "error": "already integrated with `pro-airgapped-server`. The relation should be removed before setting a resource token"
                }
            )
            return

        contract_token = event.params.get("contract-token", "")
        if not contract_token:
            event.set_results({"error": "cannot fetch the resource token: no contract token provided"})
            return
        proxies = utils.get_proxy_dict(self.config)
        contracts_url = self.config.get("contracts.url", "")
        machine_token = utils.get_machine_token(contract_token, contracts_url=contracts_url, proxies=proxies)

        if not machine_token:
            LOGGER.error("failed to retrieve the machine token")
            event.set_results({"error": "cannot fetch the resource token: failed to fetch the machine token"})
            return

        resource_token = utils.get_resource_token(machine_token, contracts_url=contracts_url, proxies=proxies)

        self._state.resource_token = resource_token

        # Action events are not deferrable, so we should pass event as None.
        self._update_workload_container_config(None)

        event.set_results({"result": "resource token set"})

    def set_status_and_log(self, msg, status) -> None:
        """Log and set unit status."""
        LOGGER.info(msg)
        self.unit.status = status(msg)

    def _get_logrotate_config(self):
        return f"""{LOG_FILE} {"{"}
            rotate 3
            daily
            compress
            delaycompress
            missingok
            notifempty
            size 10M
{"}"}
"""

    def _push_to_workload(self, filename, content, event: Optional[HookEvent]):
        """
        Create file on the workload container with the specified content.

        If the underlying event is not deferrable (e.g., an action event), the
        caller should pass None as the `event` argument.
        """
        container = self.unit.get_container(WORKLOAD_CONTAINER)
        if container.can_connect():
            LOGGER.info(f"pushing file {filename} to the workload container")
            container.push(filename, content, make_dirs=True)
        else:
            LOGGER.info("workload container not ready - deferring")
            self._defer(event)

    def _update_trusted_ca_certs(self, container: Container):
        """Update trusted CA certificates with the cert from configuration.

        Livepatch needs to restart to use newly received certificates.

        Args:
            container (Container): The workload container, the caller must ensure that we can connect.
        """
        if not self.config.get("contracts.ca"):
            LOGGER.debug("ca config not set")
            return

        try:
            cert = b64decode(self.config.get("contracts.ca")).decode("utf8")
        except Exception:
            LOGGER.error("failed to parse base64 value of `contracts.ca` config option")
            return

        container.push(TRUSTED_CA_FILENAME, cert, make_dirs=True)
        stdout, stderr = container.exec(["update-ca-certificates", "--fresh"]).wait_output()
        LOGGER.info("stdout update-ca-certificates: %s", stdout)
        LOGGER.info("stderr update-ca-certificates: %s", stderr)

    def _defer(self, event: Optional[HookEvent]):
        """
        Defer given event object if it's not None.

        This is a helper method to avoid repeating none checks. It should only
        be used when the event object can be None.
        """
        if not event:
            return
        event.defer()


if __name__ == "__main__":
    main(LivepatchCharm, use_juju_for_storage=True)
