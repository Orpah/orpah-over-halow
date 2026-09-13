/* map.js — 底图源与「本地坐标 ↔ 经纬度」换算（单一源：track.html 与 replay.html 共用）

   为什么单独一个文件（不各页抄一份）：
   ① **底图许可是合规问题，不是笔误** —— 各源条款差别极大：osm.org 官方瓦片禁止离线/预取且无 SLA、
      OpenTopoMap 要求逐字署名、Esri 是商业源…… 抄两份迟早漂移（见 ROADMAP §七）；
   ② 栅格瓦片的地名是**画在图里的像素**、没有语言参数 → 「瓦片源列表」必须按界面语言给两套
      （英文界面默认给无地名的卫星图，带地名的标出 ⚠ 提醒）；
   ③ 本地坐标 → 经纬度换算（基点 + 米/度）两页必须完全一致，否则同一条轨迹在
      「定位与轨迹」和「回放」两个页面会落在不同位置。

   **Leaflet 图层管理不在这里**：实时视图（轨迹 + 测点 + 距离环）与回放视图
   （轨迹要跨缺口断开、站位固定、另有 95% 椭圆）画法不同，各页自己管；共用的只到本文件。

   依赖：`ui_i18n.js`（T()/OrpahI18n.lang）与页面自己引入的 Leaflet；元素 id 两页统一：
   `tileSrc` / `tileUrl` / `tileNote` / `baseLat` / `baseLng`。 */

/* ---- 地图（Leaflet，用户指定瓦片源）----
   每个源：url + attr（署名，必须可见）+ note（条款说明键）+ labels（**底图地名语言**）。
     labels："local" = 用当地语言标注（中国境内即中文）/ "none" = 无地名 / "user" = 自定义自负责。
   各源许可条款差别很大，尤其 osm.org 官方瓦片禁止离线/预取，且无 SLA —— 见 ROADMAP §七。
   **列表按界面语言给不同的一套**：栅格瓦片的地名是画在图里的像素、没有语言参数，
   所以英文界面不能默认给一张“中国地图 + 中文地名”——无地名的卫星图置默认，带地名的标出提醒。 */
const TILE_PROVIDERS = {
  osm: { name: "OpenStreetMap",
         url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
         attr: "© OpenStreetMap contributors", note: "tile_note_osm", labels: "local" },
  opentopo: { name: "OpenTopoMap",
              url: "https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png",
              // OpenTopoMap 官方要求的署名文本（不能简写成 © OpenTopoMap (CC-BY-SA)）
              attr: "Map data: © OpenStreetMap contributors, SRTM | Map style: © OpenTopoMap (CC-BY-SA)",
              note: "tile_note_opentopo", labels: "local" },
  esri: { name: "Esri World Imagery",
          url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
          attr: "© Esri", note: "tile_note_esri", labels: "none" },
  custom: { i18nName: "tk_tile_custom", url: "", attr: "",
            note: "tile_note_custom", labels: "user" },
};
/* 顺序 = 列表顺序，第一项即本语言的默认。
   CartoDB Voyager 已移除：其栅格端点现在必须 API key，实测整图是 "API KEY REQUIRED" 水印。 */
const TILE_ORDER = { zh: ["osm", "opentopo", "esri", "custom"],
                     en: ["esri", "osm", "opentopo", "custom"] };
function tileLabel(key) {
  const p = TILE_PROVIDERS[key];
  let s = p.i18nName ? T(p.i18nName) : p.name;
  // 英文界面：地名为当地语言的源（中国境内会显示中文）明确标出，免得又以为是 i18n 没做
  if (OrpahI18n.lang === "en" && p.labels === "local") {
    s += " " + T("tile_labels_local_short");
  }
  return s;
}
function buildTileOptions() {
  const sel = $("tileSrc");
  if (!sel) return;
  const keys = TILE_ORDER[OrpahI18n.lang] || TILE_ORDER.zh;
  const keep = sel.value;                 // 用户选过的、仍在本语言列表里的就保留
  sel.innerHTML = "";
  for (const key of keys) {
    const o = document.createElement("option");
    o.value = key;
    o.textContent = tileLabel(key);
    sel.appendChild(o);
  }
  sel.value = keys.indexOf(keep) >= 0 ? keep : keys[0];
  refreshTileNote();
}
function refreshTileNote() {
  const el = $("tileNote");
  if (!el) return;
  const p = TILE_PROVIDERS[$("tileSrc").value] || TILE_PROVIDERS.custom;
  el.textContent = T(p.note);
}
function baseLat() { return parseFloat($("baseLat").value); }
function baseLng() { return parseFloat($("baseLng").value); }

/* ---- 基点（本地米制坐标 ↔ WGS84）：导入 / 导出 / 记忆（2026-09-13，ROADMAP §二）----

   基点是什么：本地格网（x 向东、y 向北，米）锚到经纬度的那**一个**标定输入。
   页面上的默认值只是**演示场景**（山东某地）；真机部署必须换成现场测的基点。

   **为什么要有“导入”**：现场拿到基点的形式不是让你手抄数字 —— 常见是别人给的一个
   JSON/GeoJSON 点位、或上一次导出的那份。手抄 7 位小数极易错，而**基点错 = 整条轨迹平移**
   （不是缩小，也不是"看起来还行"）：错 0.001° 纬度 ≈ 110 m。

   **单一源**：默认值、校验、存储、换算全在这里 —— 两页只放输入框与两个按钮，
   不各写一份换算（否则同一条轨迹会在两页落在不同位置）。

   **只接受 WGS84 十进制度，不做坐标系转换**：GCJ-02（国内地图偏移坐标）/BD-09/Web 墨卡托
   会被**原样当成 WGS84** → 位置偏几百米。这里**不猜**、不"智能纠正"（那会变成静默错误），
   而是在提示里写明这一条，让使用者自己确认来源。 */
const BASE_DEFAULT = { lat: 40.0777583, lng: 117.2688556 };
const BASE_KEY = "orpah.base.v1";
let baseImported = null;          // 导入的基点：{lat,lng,name,note,when}；null = 用输入框里的

/* 从**各种常见写法**里取 {lat,lng}：本项目的导出格式、`latitude/longitude`、
   `lat0/lng0`、`origin:{lat,lng}`、`base:{lat,lng}`、GeoJSON Point（`coordinates=[lng,lat]`）。
   返回 null = 一个都认不出来（**不猜**）。 */
function basePick(o) {
  if (!o || typeof o !== "object") return null;
  const num = (v) => (typeof v === "number" && isFinite(v)) ? v
    : (typeof v === "string" && v.trim() !== "" && isFinite(Number(v)) ? Number(v) : null);
  const pair = (a, b) => {
    const la = num(a), ln = num(b);
    return (la === null || ln === null) ? null : { lat: la, lng: ln };
  };
  let p = pair(o.lat, o.lng) || pair(o.latitude, o.longitude)
       || pair(o.lat0, o.lng0) || pair(o.baseLat, o.baseLng);
  if (p) return p;
  for (const k of ["origin", "base", "center", "point"]) {
    if (o[k] && typeof o[k] === "object") { p = basePick(o[k]); if (p) return p; }
  }
  if (Array.isArray(o.coordinates) && o.coordinates.length >= 2) {
    // GeoJSON 顺序是 [经度, 纬度] —— 弄反了会落到地球另一边（下面校验也拦不住，故在提示里写明）
    return pair(o.coordinates[1], o.coordinates[0]);
  }
  return null;
}
/* 取值范围校验。错误码而不是中文句子（页面按 i18n 译，保持双语一致）：
   `base_need_json`(文本解析不出对象) / `base_need_point`(认不出 lat/lng) /
   `base_bad_num`(NaN/∞) / `base_off_globe`(超出 ±90/±180)。 */
function baseCheck(b) {
  if (b === null || b === undefined) return { ok: false, err: "base_need_point" };
  const bad = (v) => !(typeof v === "number" && isFinite(v));
  if (bad(b.lat) || bad(b.lng)) return { ok: false, err: "base_bad_num" };
  if (b.lat < -90 || b.lat > 90 || b.lng < -180 || b.lng > 180) {
    return { ok: false, err: "base_off_globe", lat: b.lat, lng: b.lng };
  }
  return { ok: true, base: { lat: b.lat, lng: b.lng } };
}
/* 解析导入文本 → {ok, base, err, name, note}。**先解析、再校验、再落盘**，
   出错一律**可见**（返回错误码）—— 不静默回落成默认值（那会让人以为导入成功了）。 */
function baseParse(text) {
  let raw;
  try { raw = JSON.parse(String(text == null ? "" : text)); }
  catch (e) { return { ok: false, err: "base_bad_json", detail: String(e.message || e) }; }
  // 也接受 GeoJSON Feature / FeatureCollection 的第一个点（现场常从地图工具导出这种）
  if (raw && raw.type === "Feature" && raw.geometry) raw = raw.geometry;
  if (raw && raw.type === "FeatureCollection" && Array.isArray(raw.features)
      && raw.features.length && raw.features[0].geometry) raw = raw.features[0].geometry;
  const p = basePick(raw);
  const chk = baseCheck(p);
  if (!chk.ok) return { ok: false, err: chk.err, lat: chk.lat, lng: chk.lng };
  return { ok: true, base: chk.base,
           name: (raw && typeof raw.name === "string") ? raw.name : "",
           note: (raw && typeof raw.note === "string") ? raw.note : "" };
}
function baseStoreRead() {
  try {
    const s = window.localStorage.getItem(BASE_KEY);
    if (!s) return null;
    const o = JSON.parse(s);
    const chk = baseCheck(basePick(o));
    return chk.ok ? chk.base : null;      // 存储里坏掉就当没有（但也别把它当默认值用）
  } catch (e) { return null; }
}
function baseStoreWrite(b) {
  try {
    window.localStorage.setItem(BASE_KEY,
      JSON.stringify({ orpah_base: 1, lat: b.lat, lng: b.lng,
                       name: b.name || "", note: b.note || "", when: b.when || "" }));
    return true;
  } catch (e) { return false; }           // 隐私模式/配额满：**不抛**，如实返回 false
}
function baseStoreClear() {
  try { window.localStorage.removeItem(BASE_KEY); return true; } catch (e) { return false; }
}
/* 页面上“当前基点”的来源（用于显示，避免误读成“默认值就是现场值”）：
   `imported`（导入并存下来的）/ `manual`（手改过输入框）/ `default`（演示默认值）。 */
function baseSource() {
  const lat = baseLat(), lng = baseLng();
  const near = (a, b) => Math.abs(a - b) < 1e-9;
  if (baseImported && near(lat, baseImported.lat) && near(lng, baseImported.lng)) return "imported";
  if (near(lat, BASE_DEFAULT.lat) && near(lng, BASE_DEFAULT.lng)) return "default";
  return "manual";
}
/* 开页时把存下来的基点写进输入框（**在创建地图之前**调，否则地图会先按默认值定位）。 */
function baseApplyStored() {
  const b = baseStoreRead();
  if (!b) return null;
  baseImported = b;
  if ($("baseLat")) $("baseLat").value = String(b.lat);
  if ($("baseLng")) $("baseLng").value = String(b.lng);
  return b;
}
/* 导入：解析 → 校验 → 写输入框 → 落存储。返回 `baseParse` 的结果 + `stored`。 */
function baseImport(text) {
  const r = baseParse(text);
  if (!r.ok) return r;
  const b = { lat: r.base.lat, lng: r.base.lng, name: r.name, note: r.note,
              when: new Date().toISOString() };
  baseImported = b;
  if ($("baseLat")) $("baseLat").value = String(b.lat);
  if ($("baseLng")) $("baseLng").value = String(b.lng);
  r.stored = baseStoreWrite(b);
  return r;
}
/* 导出当前基点（与导入同格式，可原样再导回来 —— 有往返用例）。 */
function baseExportText() {
  const b = { orpah_base: 1, lat: baseLat(), lng: baseLng(),
              name: (baseImported && baseImported.name) || "",
              note: (baseImported && baseImported.note) || "",
              when: new Date().toISOString(),
              source: baseSource() };
  return JSON.stringify(b, null, 2);
}
/* 页面接线：文件选择 / 导出 / 复位 + 状态行。`onChange` 由各页给（重算地图用）。
   元素 id 两页统一：`baseFile` / `btnBaseExport` / `btnBaseReset` / `baseNote2`。 */
function baseInit(onChange) {
  const f = $("baseFile");
  const open = $("btnBaseImport");
  if (open && f) open.onclick = () => f.click();   // 文件框隐藏，按钮代点（同站位导入的做法）
  if (f) {
    f.onchange = () => {
      const file = f.files && f.files[0];
      if (!file) return;
      const rd = new FileReader();
      rd.onload = () => {
        const r = baseImport(String(rd.result || ""));
        baseShowNote(r.ok
          ? T("base_import_ok").replace("{f}", file.name)
            .replace("{lat}", r.base.lat).replace("{lng}", r.base.lng)
          : T("base_import_fail").replace("{f}", file.name).replace("{why}", baseErrText(r)));
        if (r.ok && onChange) onChange();
      };
      rd.onerror = () => baseShowNote(T("base_import_fail")
        .replace("{f}", file.name).replace("{why}", T("base_err_read")));
      rd.readAsText(file);
    };
  }
  const ex = $("btnBaseExport");
  if (ex) {
    ex.onclick = () => {
      const txt = baseExportText();
      const name = "orpah-base-" + baseLat() + "-" + baseLng() + ".json";
      const url = URL.createObjectURL(new Blob([txt], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url; a.download = name; document.body.appendChild(a); a.click();
      document.body.removeChild(a);
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      baseShowNote(T("base_export_ok").replace("{f}", name));
    };
  }
  const rs = $("btnBaseReset");
  if (rs) {
    rs.onclick = () => {
      baseStoreClear();
      baseImported = null;
      if ($("baseLat")) $("baseLat").value = String(BASE_DEFAULT.lat);
      if ($("baseLng")) $("baseLng").value = String(BASE_DEFAULT.lng);
      baseShowNote(T("base_reset_ok"));
      if (onChange) onChange();
    };
  }
  baseShowNote(T("base_src_" + baseSource()));
}
/* 错误码 → 可读文案（**不隐藏原因**：导入失败必须说清是哪一步） */
function baseErrText(r) {
  const key = r && r.err ? r.err : "base_need_point";
  let s = T(key);
  if (r && r.detail) s += "（" + r.detail + "）";
  if (r && r.err === "base_off_globe" && r.lat !== undefined) {
    s += " lat=" + r.lat + " lng=" + r.lng;
  }
  return s;
}
function baseShowNote(msg) {
  const el = $("baseNote2");
  if (el) el.textContent = msg;
}
/* 本地坐标（米，x 向东 / y 向北）→ [纬度, 经度]。
   基点由「基点纬度/经度」输入决定；米/度换算按基点纬度取经度缩放。 */
function toLatLng(x, y) {
  const lat0 = baseLat();
  const mLat = 110540, mLng = 111320 * Math.cos(lat0 * Math.PI / 180);
  return [lat0 + y / mLat, baseLng() + x / mLng];
}
function currentTileUrl() {
  const key = $("tileSrc").value;
  return key === "custom" ? $("tileUrl").value.trim() : TILE_PROVIDERS[key].url;
}

/* ---- 底图图层 + 离线回落（两页共用：各页 ensureMap() 调 addBaseLayer(map) 即可）----
   为什么需要回落：OSM 官方瓦片政策**禁止离线/预取**（见 ROADMAP §二/§七），本 demo 因此
   **不带离线底图包**；但无网时地图不能变成一片空白 —— 找人靠的是轨迹/距离环/定位估计，
   底图只是背景参考。
   做法：瓦片连续取不到且一张都没成功 → 判定离线 → 换成**本地自绘网格底图** + 比例尺 +
   明确提示（含「重试底图」）。网格是 canvas 现画的，**不含任何第三方数据** → 无许可问题。
   判据是「连续 BASE_ERR_LIMIT 张失败 且 成功数为 0」，所以个别瓦片 404（某些源在高 zoom
   没有数据）不会误判。 */
let baseMap = null, baseTiles = null, baseGrid = null, baseNote = null, baseScale = null;
let baseOffline = false, baseErr = 0, baseOk = 0;
const BASE_ERR_LIMIT = 4;

function addBaseLayer(map) {
  baseMap = map;
  baseErr = 0; baseOk = 0;
  const url = currentTileUrl();
  if (!url) { goOffline(); return; }            // 自定义源没填 URL：不用等失败
  const p = TILE_PROVIDERS[$("tileSrc").value] || TILE_PROVIDERS.custom;
  baseTiles = L.tileLayer(url, { attribution: p.attr || "", maxZoom: 19 });
  baseTiles.on("tileerror", () => {
    if (baseOffline) return;
    if (++baseErr >= BASE_ERR_LIMIT && baseOk === 0) goOffline();
  });
  baseTiles.on("tileload", () => { baseOk++; baseErr = 0; });
  baseTiles.addTo(map);
}

/* 本地自绘网格底图（深色，与页面主题一致；每 64 px 一格） */
function offlineGridLayer() {
  const Grid = L.GridLayer.extend({
    createTile() {
      const size = this.getTileSize();
      const cv = document.createElement("canvas");
      cv.width = size.x; cv.height = size.y;
      const g = cv.getContext("2d");
      g.fillStyle = "#0e131a"; g.fillRect(0, 0, size.x, size.y);
      g.strokeStyle = "#22303f"; g.lineWidth = 1;
      g.beginPath();
      for (let x = 0; x <= size.x; x += size.x / 4) {
        g.moveTo(x + 0.5, 0); g.lineTo(x + 0.5, size.y);
      }
      for (let y = 0; y <= size.y; y += size.y / 4) {
        g.moveTo(0, y + 0.5); g.lineTo(size.x, y + 0.5);
      }
      g.stroke();
      return cv;
    },
  });
  return new Grid({ maxZoom: 19 });
}

function offlineNoteControl() {
  const ctl = L.control({ position: "bottomleft" });
  ctl.onAdd = function () {
    const box = L.DomUtil.create("div");
    box.id = "mapOfflineNote";
    box.style.cssText = "background:rgba(13,17,23,.94);border:1px solid #d29922;color:#e6edf3;" +
      "padding:6px 8px;border-radius:4px;font:12px/1.5 ui-monospace,Consolas,monospace;max-width:330px";
    box.title = T("map_offline_note_title");
    box.appendChild(document.createTextNode(T("map_offline_note")));
    const b = document.createElement("button");
    b.id = "mapOfflineRetry";
    b.className = "btn";
    b.style.cssText = "display:block;margin-top:5px;cursor:pointer";
    b.textContent = T("map_offline_retry");
    b.onclick = () => retryBase();
    box.appendChild(b);
    L.DomEvent.disableClickPropagation(box);      // 点提示框/按钮不要拖动地图
    return box;
  };
  return ctl;
}

function goOffline() {
  if (baseOffline || !baseMap) return;
  baseOffline = true;
  if (baseTiles) { baseTiles.remove(); baseTiles = null; }
  baseGrid = offlineGridLayer().addTo(baseMap);
  baseScale = L.control.scale({ imperial: false, position: "bottomright" }).addTo(baseMap);
  baseNote = offlineNoteControl().addTo(baseMap);
}

function retryBase() {
  if (!baseMap) return;
  baseOffline = false; baseErr = 0; baseOk = 0;
  if (baseGrid) { baseGrid.remove(); baseGrid = null; }
  if (baseNote) { baseNote.remove(); baseNote = null; }
  addBaseLayer(baseMap);                          // 比例尺留着（离线/在线都有用）
}

function baseOfflineNow() { return baseOffline; }
