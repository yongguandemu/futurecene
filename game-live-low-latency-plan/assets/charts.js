// assets/charts.js — 游戏实况低延迟管道方案图表
(function () {
  var style = getComputedStyle(document.documentElement);
  var accent = style.getPropertyValue('--accent').trim();
  var accent2 = style.getPropertyValue('--accent2').trim();
  var ink = style.getPropertyValue('--ink').trim();
  var muted = style.getPropertyValue('--muted').trim();
  var rule = style.getPropertyValue('--rule').trim();
  var bg2 = style.getPropertyValue('--bg2').trim();
  var warn = style.getPropertyValue('--warn').trim();
  var danger = style.getPropertyValue('--danger').trim();

  // --- 图 1：单轮操作延迟构成对比（当前 vs 快环目标）---
  var chart1 = echarts.init(document.getElementById('chart-latency'), null, { renderer: 'svg' });
  chart1.setOption({
    tooltip: {
      trigger: 'axis',
      appendToBody: true,
      axisPointer: { type: 'shadow' }
    },
    legend: {
      data: ['当前实现', '快环目标'],
      textStyle: { color: ink },
      top: 0
    },
    grid: { left: 60, right: 40, top: 50, bottom: 40 },
    xAxis: {
      type: 'value',
      name: '毫秒 (ms)',
      nameTextStyle: { color: muted },
      axisLabel: { color: muted },
      axisLine: { lineStyle: { color: rule } },
      splitLine: { lineStyle: { color: rule, type: 'dashed' } }
    },
    yAxis: {
      type: 'category',
      data: ['感知轮询', '截图取帧', '视觉/OCR', '决策', '操作冷却', '注入', '合计'],
      axisLabel: { color: ink },
      axisLine: { lineStyle: { color: rule } }
    },
    series: [
      {
        name: '当前实现',
        type: 'bar',
        data: [2000, 60, 1500, 800, 3000, 40, 7400],
        itemStyle: { color: danger },
        label: {
          show: true,
          position: 'right',
          color: muted,
          formatter: function (p) {
            return p.value >= 1000 ? (p.value / 1000) + 's' : p.value + 'ms';
          }
        }
      },
      {
        name: '快环目标',
        type: 'bar',
        data: [30, 25, 100, 60, 300, 15, 530],
        itemStyle: { color: accent },
        label: {
          show: true,
          position: 'right',
          color: muted,
          formatter: function (p) { return p.value + 'ms'; }
        }
      }
    ],
    animation: false
  });
  window.addEventListener('resize', function () { chart1.resize(); });
})();
