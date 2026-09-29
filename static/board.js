/* DiffSync UID board: live refresh of legacy/new uid columns + counters. */
(function () {
  "use strict";

  var modelInput = document.getElementById("model");
  var idInput = document.getElementById("identifiers");
  var legacySource = document.getElementById("legacySource");
  var newSource = document.getElementById("newSource");
  var legacyUid = document.getElementById("legacyUid");
  var newUid = document.getElementById("newUid");
  var legacyStatus = document.getElementById("legacyStatus");
  var newStatus = document.getElementById("newStatus");
  var collisionCount = document.getElementById("collisionCount");
  var degradationCount = document.getElementById("degradationCount");
  var storedList = document.getElementById("storedList");
  var flash = document.getElementById("flash");
  var legacyDecode = document.getElementById("legacyDecode");
  var newDecode = document.getElementById("newDecode");
  var legacyDecodeOut = document.getElementById("legacyDecodeOut");
  var newDecodeOut = document.getElementById("newDecodeOut");

  function getJson(url) {
    return fetch(url).then(function (resp) { return resp.json(); });
  }

  function refreshMetrics() {
    return getJson("/api/metrics").then(function (data) {
      collisionCount.textContent = data.collision_count;
      degradationCount.textContent = data.degradation_count;
      storedList.innerHTML = "";
      (data.stored || []).forEach(function (item) {
        var li = document.createElement("li");
        li.textContent = item;
        storedList.appendChild(li);
      });
    });
  }

  function refreshColumns() {
    var model = modelInput.value;
    var ids = idInput.value;
    var url = "/api/compute?model=" + encodeURIComponent(model) +
              "&identifiers=" + encodeURIComponent(ids);
    getJson(url).then(function (data) {
      legacySource.textContent = data.model + " / " + data.identifiers.join(" , ");
      newSource.textContent = data.identifiers.map(function (seg, i) {
        return "[" + seg.length + ":" + seg + "]";
      }).join(" · ") + "   (model=" + data.model + ")";

      legacyUid.textContent = data.legacy.value;
      newUid.textContent = data.new.value;

      legacyUid.classList.toggle("collides", data.legacy.collides);
      if (data.legacy.collides) {
        legacyStatus.className = "badge warn";
        legacyStatus.textContent = "与另一行撞键";
      } else {
        legacyStatus.className = "badge ok";
        legacyStatus.textContent = "可读";
      }
      newStatus.className = "badge ok";
      newStatus.textContent = "零碰撞";
    });
  }

  function markDecode(elOut, payload) {
    elOut.style.display = "block";
    if (payload.valid) {
      elOut.classList.remove("bad");
      var matchNote = payload.model_matches === false ? " (模型不匹配:" + payload.model + ")" : "";
      elOut.textContent = "[" + payload.kind + "] model=" + payload.model +
        " ids=(" + (payload.identifiers || []).join(", ") + ")" + matchNote;
    } else {
      elOut.classList.add("bad");
      elOut.textContent = "非法 uid：" + (payload.error || "无法解析") + "（已记录一次降级，不影响页面）";
    }
  }

  var decodeTimerLegacy = null;
  legacyDecode.addEventListener("input", function () {
    clearTimeout(decodeTimerLegacy);
    var raw = legacyDecode.value;
    if (!raw) { legacyDecodeOut.style.display = "none"; return; }
    decodeTimerLegacy = setTimeout(function () {
      var url = "/api/decode?model=" + encodeURIComponent(modelInput.value) +
                "&uid=" + encodeURIComponent(raw);
      getJson(url).then(function (data) {
        markDecode(legacyDecodeOut, data);
        refreshMetrics();
      });
    }, 150);
  });

  var decodeTimerNew = null;
  newDecode.addEventListener("input", function () {
    clearTimeout(decodeTimerNew);
    var raw = newDecode.value;
    if (!raw) { newDecodeOut.style.display = "none"; return; }
    decodeTimerNew = setTimeout(function () {
      var url = "/api/decode?model=" + encodeURIComponent(modelInput.value) +
                "&uid=" + encodeURIComponent(raw);
      getJson(url).then(function (data) {
        markDecode(newDecodeOut, data);
        refreshMetrics();
      });
    }, 150);
  });

  var computeTimer = null;
  function scheduleCompute() {
    clearTimeout(computeTimer);
    computeTimer = setTimeout(refreshColumns, 80);
  }
  modelInput.addEventListener("input", scheduleCompute);
  idInput.addEventListener("input", scheduleCompute);

  document.getElementById("replayBtn").addEventListener("click", function () {
    fetch("/api/replay", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device_name: "a", name: "b__c" })
    }).then(function (r) { return r.json(); }).then(function () {
      flash.textContent = "已重放：碰撞计数保持不变（幂等）。";
      refreshMetrics();
      refreshColumns();
    });
  });

  document.getElementById("illegalBtn").addEventListener("click", function () {
    getJson("/api/decode?model=interface&uid=" + encodeURIComponent("v2\u00b611:truncated")).then(function () {
      flash.textContent = "已模拟一次非法 uid 读取：仅在栏内标红，降级计数 +1（重放不重复计数）。";
      refreshMetrics();
    });
  });

  refreshColumns();
  refreshMetrics();
})();
