# Factorio Scenarios

This directory is the local staging area for Factorio scenario source code.
Git tracks only this README; all scenario directories and their contents are
ignored. Each immediate child directory is one scenario that Ansible can
deploy to a configured server instance.

## Structure

A scenario directory must contain a `control.lua` entry point. It may also
contain supporting Lua modules, locale files, maps, migrations, and its own
documentation.

```text
scenarios/
  ExampleScenario/
    control.lua
    description.json
    locale/
    modules/
```

Keep runtime saves, exported worlds, credentials, and server backups out of
this directory. Saves belong in the ignored top-level `saves/` directory when
they are needed for a one-time deployment. Scenario source remains local and
must be backed up separately if it needs to be preserved.

## Deploying A Scenario

Set an instance's `scenario_name` in
the ignored local `ansible/group_vars/instances_custom.yml` (created from
`instances_custom.example.yml`) to the child directory name:

```yaml
factorio_custom_instances:
  - id: example
    scenario_name: ExampleScenario
```

The Factorio Ansible role copies that directory to the instance's
`<state_dir>/scenarios/` directory as root-owned, read-only source. When the
instance has no save, its launcher starts the configured scenario. Once
Factorio creates a save, later starts load that save instead of generating the
scenario again.

Changing scenario source does not rewrite an existing world's embedded state.
Apply changes through the scenario's migration mechanism when it has one, or
reset/create a world when the change requires a fresh scenario start.

## Adding A Scenario

1. Add a uniquely named local child directory with `control.lua`.
2. Include a scenario-level README with dependencies and compatibility notes.
3. Configure an instance's `scenario_name` to match the directory exactly.
4. Run the Ansible syntax check and deploy the Factorio role.
5. Test with a new or intentionally migrated save before using it in production.