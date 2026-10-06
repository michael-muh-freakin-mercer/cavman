# Moving CI off Ubuntu 24.04

Every CI job is pinned to `ubuntu-24.04`. GitHub moves `ubuntu-latest` to
Ubuntu 26.04 between 2026-10-19 and 2026-11-19, and 24.04 stays available
after that, so nothing breaks on its own. The move is still worth making
deliberately, because cavman.dev's Droplet will eventually run 26.04 too.

## What depends on the runner image

| Dependency | Where | Risk on 26.04 |
| --- | --- | --- |
| Distribution Python, used as the sandbox interpreter (`python3 -m venv`) | `suite`, `e2e`, `live-smoke`, `e2b` | 26.04 ships Python 3.14. The sandbox binds `/usr` into a cleared environment, so the venv must still work there. Dependencies need 3.14 wheels. |
| `bubblewrap`, `libseccomp2`, `prlimit` from apt | every job that runs candidate code | Newer versions. The seccomp network filter loads `libseccomp.so.2` by name. |
| `kernel.apparmor_restrict_unprivileged_userns` | same | Still the default on 26.04. The `sysctl` step must keep working, or bubblewrap fails closed. |
| cgroup v2 only | worker resource monitoring | The monitor reads `/proc`, not cgroups, but this is worth confirming. |
| Docker and Compose | `deploy`, `backup`, `log shipper` jobs | Compose v2 syntax (`!reset`, `!override`) needs a recent Compose. |
| Playwright browser dependencies (`--with-deps`) | `e2e`, `deploy` | Pinned Playwright 1.56 must support 26.04 package names. |

## The plan

1. **Now:** the advisory job `backend suite on Ubuntu 26.04` runs the offline
   suite on `ubuntu-26.04` on every push. It cannot fail the build. Read its
   result on each PR.
2. **When it is green for a week:** in one PR, add 26.04 runs of `e2e` and
   `deploy` (also advisory), and fix whatever they find.
3. **When those are green:** in one commit, change every `runs-on:
   ubuntu-24.04` to `ubuntu-26.04`, and remove the advisory jobs. Required
   check names do not change, so the branch ruleset needs no edit.
4. **Afterwards:** plan the Droplet (production) upgrade separately. It is an
   operating-system upgrade of a live server and needs the owner.

## Rolling back

Revert the one commit from step 3. Nothing else depends on the image.
