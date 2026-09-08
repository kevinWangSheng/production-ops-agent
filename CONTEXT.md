# Operations Investigation

Language for the read-only investigation product and its human-operated incident lifecycle.

## Language

**Investigated environment**:
The services and operational data the Agent is permitted to observe. The Agent's own incident records are separate from this environment.

**Incident**:
A tracked service problem joining related signals, evidence, investigation history and human handling.

**Investigation**:
The process of acquiring and testing evidence about an incident or observed release, with an explicit completion or handoff state.

**Post-release investigation**:
Investigation of an already deployed revision and its subsequent behavior. It is advisory and has no release-gate authority.
_Avoid_: Release executor, release approver

**Evidence Packet**:
The inspectable record of observations, provenance, hypotheses, counter-evidence, uncertainty and findings for an investigation.

**Recovery observation**:
A conclusion about current service health after human handling, grounded in independent observations over time.
_Avoid_: Automatic remediation

**Postmortem draft**:
A reviewable account of impact, timeline, supported findings, uncertainty, human handling and recovery; it is not automatically trusted knowledge.

**Reviewed knowledge**:
Reusable investigation guidance with recorded provenance and explicit human confirmation.

**IncidentScenario / IncidentOutcome**:
The external test input and observable result of an incident workflow, including evidence, state, boundaries and service observations.

**Release observation**:
A bounded record of an already deployed revision and its subsequent health, whether or not an incident exists. An abnormal result can link to an incident; a healthy release is not itself an incident.

**Investigation Run**:
One bounded execution of an investigation for an incident or release observation. Its execution status is distinct from whether its conclusion is supported or the service has recovered.

**Observation session**:
An explicitly authorized period of independent health observation for an incident's recovery or a release. Its observations belong to that subject and stage, not to another investigation's authority.

**Health profile**:
The reviewed, versioned criteria that make a target's health observations meaningful, including required signals, sufficient samples, freshness and missing-data behavior.
