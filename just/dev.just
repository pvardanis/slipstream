# Developer tooling for working on the repo with Claude Code.

# Start the read-only Grafana MCP server over stdio against the Grafana Cloud stack;
# Claude Code's project .mcp.json runs it. Requires `just grafana-cloud-setup` done.
[group('dev')]
grafana-mcp: _bootstrap-init
    {{ justfile_directory() }}/scripts/grafana-mcp.sh {{ bootstrap_dir }} {{ grafana_dir }}
