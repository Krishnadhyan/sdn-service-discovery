# T3 Test Report (observed session notes)

Date of observed Mininet session: 2026-10-11. These results were reported from the user's Ubuntu/Mininet terminal during integration. They are not a claim that every acceptance item or the newly added browser dashboard has been tested end-to-end.

## Manually observed and passed

| Scenario | Evidence observed | Result |
|---|---|---|
| FILE normal request | Client discovered FILE-1 and FILE-2; FILE-1 returned `FILE-1 OK service=FILE request=PING` | PASS |
| FILE-1 heartbeat timeout | Registry logged `FILE-1 timed out -> INACTIVE`; controller event returned HTTP 200 | PASS |
| FILE-1 OpenFlow block | `ovs-ofctl dump-flows s2` showed priority 100 TCP destination 10.0.0.4 / port 5001 drop rule | PASS |
| FILE failover | Client discovered only FILE-2 and received a FILE-2 response | PASS |
| FILE-1 recovery | Registry logged re-registration, `FILE-1 is ACTIVE again`, and controller event HTTP 200 | PASS |
| FILE drop-rule removal | Flow table after recovery showed only the priority-0 NORMAL rule | PASS |
| FILE request after recovery | Client discovered FILE-1 and FILE-2 and received FILE-1 response | PASS |
| CHAT failure | Registry logged CHAT-1 timeout and controller HTTP 200; client made three discovery attempts then returned CHAT service unavailable | PASS |
| VIDEO failure | Registry logged VIDEO-1 timeout and controller HTTP 200; client made three discovery attempts then returned VIDEO service unavailable | PASS |
| VIDEO recovery | User confirmed VIDEO recovery passed | PASS (user-confirmed; detailed output not included here) |
| Normal FILE/VIDEO regression | FILE and VIDEO client requests succeeded after recovery | PASS |
| Normal CHAT regression | CHAT-1 response `CHAT-1 OK service=CHAT request=PING` observed after restart | PASS |

## Dashboard verification and remaining checks

The live frontend dashboard was exercised in the Ubuntu/Mininet VM on 2026-10-11.

| Dashboard check | Observed result |
|---|---|
| Registry connection and provider inventory | Registry connected; FILE-1, FILE-2, CHAT-1, and VIDEO-1 displayed ACTIVE |
| Direct provider communication | FILE-1 responded successfully to PING; CHAT and VIDEO PING success was also reported |
| FILE-1 failure and failover | Dashboard showed FILE-1 INACTIVE; FILE discovery selected FILE-2 |
| OpenFlow during FILE-1 failure | Priority-100 TCP destination 10.0.0.4, port 5001 drop rule observed |
| FILE-1 recovery | Provider returned ACTIVE and became discoverable again |
| OpenFlow after recovery | Priority-100 drop rule was removed; priority-0 NORMAL rule remained |
| Event timeline | Registry registration, timeout, recovery, and controller HTTP 200 events displayed successfully |

### Remaining final checks

- Re-run the automated tests against the final packaged project on the target Ubuntu VM and retain the output.
- Verify CHAT and VIDEO failure/recovery evidence is included in the handover evidence package.
- Confirm `ovs-ofctl` is available to the dashboard process. A query error must not be interpreted as proof that no flow rules exist.
- The event timeline requires a real Registry log file. The tested dashboard reads `/tmp/registry.log` by default; set `REGISTRY_LOG` if the log is stored elsewhere.

## Commands for final automated check

```bash
python3 -m unittest discover -s registry/tests -v
python3 -m unittest discover -s tests -v
python3 -m compileall -q providers client frontend registry integration tests
```
