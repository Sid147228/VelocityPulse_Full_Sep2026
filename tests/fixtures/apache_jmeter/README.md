# Apache JMeter compatibility fixtures

These fixtures are copied from the public Apache JMeter repository and are used
only to regression-test VelocityPulse's JMeter-compatible reporting maths.

Source files:
- `bin/testfiles/HTMLReportTestFile.csv`
- `src/dist-check/src/test/resources/org/apache/jmeter/gui/report/HTMLReportExpect.json`

Upstream project: Apache JMeter
License: Apache License 2.0

Apache JMeter's own report-generator tests compare the generated
`statistics.json` with `HTMLReportExpect.json`. VelocityPulse uses the same
fixture to make sure its selected transaction metrics reproduce JMeter values.
