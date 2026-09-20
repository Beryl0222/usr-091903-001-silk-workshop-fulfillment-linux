"use strict";

const { spawnSync } = require("node:child_process");

const suites = ["service_contract", "test_domain", "test_api"];
const failed = [];
for (const suite of suites) {
  const result = spawnSync("python3", ["-m", "unittest", "-v", suite], { stdio: "inherit" });
  if (result.error) {
    console.error(result.error.message);
    process.exit(1);
  }
  if (result.status !== 0) failed.push(suite);
}
if (failed.length) {
  console.error(`测试失败: ${failed.join(", ")}`);
  process.exit(1);
}
console.log("全部测试通过");
