# openspec/changes/

Active SDD changes for Primer RAG are tracked here. During `sdd-init` we only
create the skeleton.

Lifecycle (per openspec-convention.md):

```
proposal  ->  specs/  ->  design  ->  tasks  ->  apply  ->  verify  ->  archive
                                                                  |
                                                                  v
                                              changes/archive/YYYY-MM-DD-<name>/
```

A change folder looks like:

```
openspec/changes/<change-name>/
  state.yaml          # DAG state (survives compaction)
  exploration.md      # optional, from sdd-explore
  proposal.md         # from sdd-propose
  specs/<domain>/spec.md   # from sdd-spec
  design.md           # from sdd-design
  tasks.md            # from sdd-tasks, updated by sdd-apply
  verify-report.md    # from sdd-verify
```

No active change exists yet. Next recommended step is `/sdd-new <name>` or
`/sdd-explore <topic>` to start one.