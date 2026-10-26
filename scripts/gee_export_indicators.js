/**
 * RouteGuard · 指标反演导出脚本（Google Earth Engine）
 * -----------------------------------------------------
 * 输入：一个 FeatureCollection，每个 feature 是一段轨迹缓冲区（segment），
 *       属性含 segment_id，几何为 LineString 或缓冲后的 Polygon。
 * 输出：CSV（每个 segment 一行，含各指标中位数与质量标记）。
 *
 * 使用前必须确认三件事：
 *   1) 你的 GEE 账号已开通（学术用途免费），并选好 export 的 Drive 目录；
 *   2) CANOPY_ASSET 指向可用的冠层高度产品（不同账号可见性不同，见下）；
 *   3) 影像时间窗与你的实验日期对齐。
 *
 * 论文里要写清楚：所有指标都是"某时间窗内的合成值"，不是骑行当日实测。
 */

// ============ 配置区 ============
var SEGMENTS = 'projects/your-project/assets/routeguard_segments'; // TODO 改成你的资产
var START = '2026-07-01';
var END = '2026-08-31';
var CLOUD_PCT = 30;

// 冠层高度候选（按可用性从上到下尝试）：
//   Meta & WRI Global Canopy Height 1m (2024) —— 需确认在你账号下可见
//   ETH Global Canopy Height 10m (2020)      —— 覆盖全球，较稳
// 若都不可用，退回 NDVI 估算并在论文中说明局限。
var CANOPY_ASSET = 'projects/meta-forest-monitoring-okw37/assets/CanopyHeight'; // TODO 核实
var USE_CANOPY = true;

var segs = ee.FeatureCollection(SEGMENTS);

// ============ Sentinel-2：NDVI / NDBI / 云掩膜 ============
var s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
  .filterDate(START, END)
  .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', CLOUD_PCT))
  .map(function (img) {
    var scl = img.select('SCL');
    // 4 植被 5 裸土 6 水 7 低概率云 11 雪 —— 保留可用地表像元
    var mask = scl.eq(4).or(scl.eq(5)).or(scl.eq(6));
    return img.updateMask(mask)
      .addBands(img.normalizedDifference(['B8', 'B4']).rename('NDVI'))
      .addBands(img.normalizedDifference(['B11', 'B8']).rename('NDBI'))
      .copyProperties(img, ['system:time_start']);
  });

var s2med = s2.median();
// 质量：用有效观测数近似（云掩膜后剩余影像数）
var s2count = s2.select('NDVI').count().rename('S2_COUNT');

// ============ Landsat C2 L2：地表温度 ============
// ST_B10 为开尔文 ×0.00341802 + 149.0
var lst = ee.ImageCollection('LANDSAT/LC08/C02/T1_L2')
  .merge(ee.ImageCollection('LANDSAT/LC09/C02/T1_L2'))
  .filterDate(START, END)
  .filter(ee.Filter.lt('CLOUD_COVER', CLOUD_PCT))
  .map(function (img) {
    var st = img.select('ST_B10')
      .multiply(0.00341802).add(149.0).subtract(273.15)
      .rename('LST_C');
    return st.copyProperties(img, ['system:time_start']);
  })
  .median();

// ============ Copernicus DEM：坡度 ============
var dem = ee.Image('COPERNICUS/DEM/GLO30').select('DEM');
var slope = ee.Terrain.slope(dem).rename('SLOPE_DEG');

// ============ 冠层高度（可选） ============
var canopy = USE_CANOPY
  ? ee.Image(CANOPY_ASSET).select(0).rename('CANOPY_H_M')
  : ee.Image(0).rename('CANOPY_H_M');

// ============ 合成并分区统计 ============
var stack = s2med.select(['NDVI', 'NDBI'])
  .addBands(s2count)
  .addBands(lst)
  .addBands(slope)
  .addBands(canopy);

var stats = stack.reduceRegions({
  collection: segs,
  reducer: ee.Reducer.median(),
  scale: 10,
  tileScale: 4
});

// 同时导出行数，用于质量标记
var countStats = s2count.reduceRegions({
  collection: segs,
  reducer: ee.Reducer.median(),
  scale: 10,
  tileScale: 4
});

Export.table.toDrive({
  collection: stats,
  description: 'routeguard_indicators',
  folder: 'routeguard',
  fileNamePrefix: 'indicators',
  fileFormat: 'CSV'
});

print('segments:', segs.size());
print('sample row:', stats.first());
