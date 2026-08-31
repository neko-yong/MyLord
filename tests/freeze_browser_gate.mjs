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
