// Browser-skill SDK helper. Supply a logged-in loopback fixture tab, not a production tab.
// This checks real clicks/DOM; it does not mutate Streamlit session_state or tab.open.
export async function navigationGate(tab, cycles = 20) {
  const sequence = [
    ["② 争议地图", "争议地图"], ["③ 调解室", "共享调解室"],
    ["② 争议地图", "争议地图"], ["③ 调解室", "共享调解室"],
    ["④ 最终仲裁", "最终仲裁"], ["③ 调解室", "共享调解室"],
  ];
  const initial = await tab.playwright.domSnapshot();
  if (!initial.includes("LOCAL FIXTURE ONLY") || !initial.includes("当前身份：A")) {
    throw new Error("Use an authenticated A loopback fixture session.");
  }
  const results = [];
  for (let cycle = 0; cycle < cycles; cycle++) {
    for (const [label, heading] of sequence) {
      await tab.playwright.getByRole("tab", { name: label, exact: true }).click();
      let snapshot = await tab.playwright.domSnapshot();
      // Streamlit may still be processing the click on the first DOM observation.
      for (let attempt = 0; attempt < 3 && !snapshot.includes(`heading "${heading}"`); attempt++) {
        snapshot = await tab.playwright.domSnapshot();
      }
      const paths = (snapshot.match(/heading "(你的独立陈述|争议地图|共享调解室|最终仲裁)"/g) || []).length;
      const pass = paths === 1 && snapshot.includes(`heading "${heading}"`)
        && snapshot.includes("当前身份：A") && snapshot.includes("当前案件：");
      results.push({ cycle, label, paths, pass });
      if (!pass) throw new Error(`Navigation failed at cycle ${cycle + 1}, ${label}`);
    }
  }
  return results;
}

// Unlike navigationGate, do not wait for the content to settle between inputs.
// Interrupted renders are expected; inspect settled DOM separately after a burst.
export async function rapidNavigationGate(tab, cycles = 5) {
  const initial = await tab.playwright.domSnapshot();
  if (!initial.includes("LOCAL FIXTURE ONLY") || !initial.includes("当前身份：A")) {
    throw new Error("Use an authenticated A loopback fixture session.");
  }
  const results = [];
  for (let cycle = 0; cycle < cycles; cycle++) {
    for (const label of ["② 争议地图", "③ 调解室", "② 争议地图", "③ 调解室", "④ 最终仲裁", "③ 调解室"]) {
      const target = tab.playwright.getByRole("tab", { name: label, exact: true });
      await target.click();
      const accepted = await target.getAttribute("aria-selected") === "true";
      results.push({ at: Date.now(), cycle, label, accepted });
      if (!accepted) throw new Error(`Click did not select ${label}; check dialogs/running UI`);
    }
  }
  return results;
}

// Start both tabs at a fresh collecting fixture. A 4s post-commit delay on B's
// save makes A the map generator; a real tab click then interrupts that run.
export async function mapOwnerInterruptionGate(a, b) {
  const first = await a.playwright.domSnapshot();
  const caseId = first.match(/LOCAL FIXTURE ONLY — Case ID: (DEV-[A-Z0-9]+)/)?.[1];
  if (!caseId || !(await b.playwright.domSnapshot()).includes(caseId)) {
    throw new Error("Use two fresh collecting loopback fixture tabs for the same case.");
  }
  const fields = [
    "1. 事情是怎么开始 / 发生的？（必填）", "3. 对方哪些具体行为让你不满？（必填）",
    "4. 你当时具体做了什么？（必填）", "6. 你真正需要 / 在意的是什么？（必填）",
    "7. 你希望对方做什么 / 希望这次解决什么？（必填）",
  ];
  const observations = [];
  for (const [tab, role] of [[a, "A"], [b, "B"]]) {
    await tab.playwright.getByRole("textbox", { name: "Case ID", exact: true }).fill(caseId);
    await tab.playwright.getByRole("textbox", { name: "个人密钥", exact: true }).fill(`${role}-browser-fixture`);
    await tab.playwright.getByRole("button", { name: "login 进入案件", exact: true }).click();
    await tab.playwright.domSnapshot();
    for (const name of fields) {
      await tab.playwright.getByRole("textbox", { name, exact: true }).fill("Synthetic statement for ownership race");
    }
    await tab.playwright.getByRole("button", { name: "lock 提交并冻结", exact: true }).click();
    const pending = await tab.playwright.domSnapshot();
    if (!pending.includes('dialog "warning 确认操作"')) throw new Error("Expected real confirmation dialog");
    await tab.playwright.getByRole("button", { name: "lock 确认提交", exact: true }).click();
    const confirmed = await tab.playwright.domSnapshot();
    observations.push({ at: Date.now(), role, authenticated: confirmed.includes(`当前身份：${role}`) });
  }
  for (const name of ["② 争议地图", "③ 调解室"]) {
    const target = a.playwright.getByRole("tab", { name, exact: true });
    await target.click();
    const accepted = await target.getAttribute("aria-selected") === "true";
    observations.push({ at: Date.now(), name, accepted });
    if (!accepted) throw new Error("Owner click was blocked; do not count as an interruption");
  }
  return observations;
}
