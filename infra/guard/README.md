# Infrastructure security checks

These rules come from the AWS CloudFormation Guard Rules Registry. `source.json`
records the exact upstream commit and file paths. The upstream license is included.

Selected controls cover S3 public access, encryption, HTTPS-only requests,
versioning, IAM full-access/pass-role policies, and public Lambda permissions.
Some upstream IAM rules inspect standalone policies rather than inline role
policies; a SKIP is not proof that an inline policy is secure. Scoped deployment
roles are reviewed separately using IAM Policy Autopilot and actual deployment
permission failures. These selected checks are not a full compliance assessment.

Run Guard 3.3.0 with:

```text
cfn-guard validate --rules infra/guard --data infra/foundation.yaml --output-format json
cfn-guard validate --rules infra/guard --data infra/application.yaml --output-format json
```
