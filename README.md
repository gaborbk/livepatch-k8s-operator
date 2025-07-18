# Canonical Livepatch Server (K8s Charm)

[![CharmHub Badge](https://charmhub.io/canonical-livepatch-server-k8s/badge.svg)](https://charmhub.io/canonical-livepatch-server-k8s)
[![Release](https://github.com/canonical/livepatch-k8s-operator/actions/workflows/publish_charm.yaml/badge.svg)](https://github.com/canonical/livepatch-k8s-operator/actions/workflows/publish_charm.yaml)
[![Tests](https://github.com/canonical/livepatch-k8s-operator/actions/workflows/test.yaml/badge.svg?branch=main)](https://github.com/canonical/livepatch-k8s-operator/actions/workflows/test.yaml?query=branch%3Amain)

## Description

The Livepatch K8s charm is the easiest and the recommended way to deploy the Livepatch server on K8s. This charm configures and runs the Livepatch server, which serves Livepatch-es and metadata attached to them to the clients. Canonical Livepatch patches high and critical linux kernel vulnerabilities, removing the immediate need to reboot to upgrade the kernel, instead allowing the downtime to be scheduled. It is a part of the Ubuntu Pro offering.

⚠️ For users who want to deploy an entire Livepatch on-prem server (including its dependencies), it is recommended to use the `k8s/stable` channel of the [bundle](https://charmhub.io/canonical-livepatch-onprem?channel=k8s/stable) made for this purpose. For more detailed steps on using the bundle, please see the [tutorials](https://ubuntu.com/security/livepatch/docs/livepatch_on_prem/tutorial) on the Livepatch website.

## Usage

The Livepatch server may be deployed using the Juju command line as follows:

```sh
juju deploy canonical-livepatch-server-k8s
```

## Integrations

### Database

Livepatch server requires integration with a PostgreSQL charm via the `database` endpoint. As an example, users can deploy a [PostgreSQL](https://charmhub.io/postgresql-k8s) database and integrate it with Livepatch as follows:

```sh
juju deploy postgresql-k8s --trust
juju integrate canonical-livepatch-server-k8s:database postgresql-k8s:database
```

There is also an endpoint, named `database-legacy`, which can be used with PostgreSQL charm's legacy endpoint, `db` . But it is strongly recommended that users integrate with the `database` endpoint mentioned earlier.

### Nginx ingress (`nginx-route`)

Livepatch provides an endpoint, named `nginx-route`, which can be integrated with the [`nginx-ingress-integrator`](https://charmhub.io/nginx-ingress-integrator) charm to expose the Livepatch server via an Nginx-controlled cluster ingress. As an example, users can integrate with this endpoint by using Juju as follows:

```sh
juju integrate canonical-livepatch-server-k8s:nginx-route nginx-ingress-integrator:nginx-route
```

### Loki (optional)

Livepatch can be optionally integrated with [Loki](https://charmhub.io/loki-k8s) via the `log-proxy` endpoint. Users can integrate other applications with this endpoint by using Juju as follows:

```sh
juju integrate canonical-livepatch-server-k8s:log-proxy loki-k8s:logging
```

### Grafana dashboard (optional, provides)

Livepatch provides observability dashboards on Grafana. Users can monitor the status of the running Livepatch server via many metrics, including (but not limited to):
- Rate of HTTP response status codes (e.g., 200, 404, or 403) for a range of percentile values.
- Rate of incoming HTTP requests per second.
- Database metrics (i.e., errors and response times).

For this purpose, there is an endpoint, named `grafana-dashboard`, which implements the `grafana_dashboard` interface and can be integrated with [Grafana](https://charmhub.io/grafana-k8s). Users can integrate other applications with this endpoint by using Juju as follows:

```sh
juju integrate canonical-livepatch-server-k8s:grafana-dashboard grafana-k8s:grafana-dashboard
```

### Prometheus (optional, provides)

Users can integrate Livepatch server with Prometheus to have it scrape the metrics. For this purpose, there is an endpoint, named `metrics-endpoint`, which implements the `prometheus_scrape` interface and can be integrated with [Prometheus](https://charmhub.io/prometheus-k8s). Users can integrate other applications with this endpoint by using Juju as follows:

```sh
juju integrate canonical-livepatch-server-k8s:metrics-endpoint prometheus-k8s:metrics-endpoint
```

### Air-gapped Contracts (optional, requires)

Livepatch can be optionally integrated with [`pro-airgapped-server`](https://charmhub.io/pro-airgapped-server) via the `pro-airgapped-server` endpoint. Note that since the `pro-airgapped-server` is a machine charm, a cross-model relation is needed. Assuming there is a SaaS named `pro-airgapped-server` in the model, the integration can be done by using this command:

```sh
juju integrate canonical-livepatch-server:pro-airgapped-server pro-airgapped-server
```

### Livepatch CVE service (optional, requires)

Livepatch can be optionally integrated with [Livepatch CVE service](https://charmhub.io/canonical-livepatch-cve-k8s) via the `cve-catalog` endpoint. This integration provides the Livepatch server with the information about CVEs fixed in Ubuntu kernels. The integration can be done by using this command:

```sh
juju integrate canonical-livepatch-server canonical-livepatch-cve-k8s
```

## OCI Image

This charm uses an OCI image, built as a ROCK and published on GitHub Container Registry (GHCR) as `ghcr.io/canonical/livepatch-server`.

## Documentation

For more detailed instructions on deploying Livepatch server, please see the documentation for this service, available on the [Livepatch website](https://ubuntu.com/security/livepatch/docs).

## Contributing

Please see the [Juju SDK documentation](https://juju.is/docs/sdk) for more information about developing and improving charms and [Contributing](CONTRIBUTING.md) for developer guidance.

## License

The Livepatch K8s charm is free software, distributed under the Apache Software License, version 2.0. See [License](LICENSE) for more details.
