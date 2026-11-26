(async () => {
  const semester = "2026-2027-1";
  const match = /^(\d{4})-(\d{4})-([123])$/.exec(semester);
  if (!match) throw new Error("学期格式应为 2026-2027-1");
  const suffix = { "1": "09", "2": "02", "3": "06" }[match[3]];
  const code = `${match[3] === "1" ? match[1] : match[2]}${suffix}`;
  const origin = "https://yjs.sjtu.edu.cn";
  if (!location.href.startsWith(`${origin}/`)) {
    throw new Error("请先在内置浏览器打开并登录研究生教学信息平台的我的课表页面");
  }

  async function post(path, parameters) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(`${origin}${path}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
          "X-Requested-With": "XMLHttpRequest",
        },
        body: new URLSearchParams(parameters),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`研究生课表接口返回 ${response.status}`);
      const payload = await response.json();
      if (payload.code !== "0") throw new Error("研究生课表接口返回失败，请检查登录状态");
      return payload;
    } finally {
      clearTimeout(timer);
    }
  }

  const [schedule, courses] = await Promise.all([
    post("/gsapp/sys/wdkbapp/modules/xskcb/xspkjgcx.do", { XNXQDM: code, XH: "" }),
    post("/gsapp/sys/wdkbapp/modules/xskcb/xsjxrwcx.do", {
      XNXQDM: code,
      XH: "",
      pageNumber: "1",
      pageSize: "999",
    }),
  ]);
  const scheduleRows = schedule.datas.xspkjgcx.rows.map((row) => ({
    BJDM: row.BJDM,
    BJMC: row.BJMC,
    JASMC: row.JASMC,
    JSJCDM: row.JSJCDM,
    JSXM: row.JSXM,
    KBBZ: row.KBBZ,
    KCDM: row.KCDM,
    KCMC: row.KCMC,
    KSJCDM: row.KSJCDM,
    WID: row.WID,
    XQ: row.XQ,
    ZCBH: row.ZCBH,
    ZCMC: row.ZCMC,
  }));
  const courseRows = courses.datas.xsjxrwcx.rows.map((row) => ({
    BJDM: row.BJDM,
    BJMC: row.BJMC,
    KCDM: row.KCDM,
    KCMC: row.KCMC,
    RKJS: row.RKJS,
    SCSKRQ: row.SCSKRQ,
    XKBZ: row.XKBZ,
    XQDM_DISPLAY: row.XQDM_DISPLAY,
  }));
  return { semester, scheduleRows, courseRows };
})()
