/* diffsync UID board: live left/right comparison, refetched on every keystroke. */
(function () {
  "use strict";

  var modelInput = document.getElementById("model");
  var fieldsInput = document.getElementById("fields");
  var uidsInput = document.getElementById("uids");
  var statusEl = document.getElementById("status");
  var timer = null;

  function setText(id, value) {
    document.getElementById(id).textContent = value;
  }

  function renderLeft(data) {
    var left = data.left;
    var legacyBox = document.getElementById("left-legacy");
    var newBox = document.getElementById("left-new");

    setText("left-legacy-text", left.legacy_uid);
    setText("left-legacy-hint", "历史 __ 拼接，已有缓存/历史 diff 原样读回");
    setText("left-new-text", left.new_uid);
    setText("left-new-fields", "解码回字段: [" + left.decoded_fields.map(function (f) {
      return JSON.stringify(f);
    }).join(", ") + "]  roundtrip=" + (left.new_decodes ? "OK" : "FAIL"));

    newBox.classList.toggle("invalid", !left.new_decodes);

    var note = document.getElementById("left-collision");
    var collisions = data.board_collisions || {};
    if (Object.keys(collisions).length) {
      note.hidden = false;
      note.textContent = "检测到旧 uid 碰撞: " + Object.keys(collisions).map(function (k) {
        return k + " -> " + collisions[k].length + " 个不同新 uid";
      }).join("; ");
    } else {
      note.hidden = true;
    }
    legacyBox._uid = left.legacy_uid;
  }

  function uidRow(item) {
    var box = document.createElement("div");
    box.className = "uid-box " + item.scheme;
    var tag = document.createElement("span");
    tag.className = "tag " + item.scheme;
    tag.textContent = item.scheme === "new" ? "新" : item.scheme === "legacy" ? "旧" : "非法";
    var body = document.createElement("span");
    body.textContent = item.uid;
    box.appendChild(tag);
    box.appendChild(body);

    var detail = document.createElement("div");
    detail.className = "muted";
    if (item.invalid) {
      detail.textContent = "非法新 uid：无法按长度前缀解析，仅在本栏标红，不中断其它内容。";
      detail.style.color = "var(--bad)";
    } else if (item.scheme === "legacy") {
      detail.textContent = "旧 uid 原样保留: " + item.legacy_literal;
    } else {
      detail.textContent = "解码字段: [" + item.fields.map(function (f) {
        return JSON.stringify(f);
      }).join(", ") + "]";
    }
    box.appendChild(detail);
    return box;
  }

  function renderRight(data) {
    var container = document.getElementById("right-results");
    container.innerHTML = "";
    (data.right || []).forEach(function (item) {
      container.appendChild(uidRow(item));
    });
  }

  function renderMetrics(data) {
    setText("collision-count", data.metrics.collision_count);
    setText("degradation-count", data.metrics.degradation_count);
    setText("metrics-json", JSON.stringify(data.metrics, null, 2));
  }

  function refresh() {
    statusEl.textContent = "刷新中…";
    fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        model: modelInput.value,
        fields: fieldsInput.value,
        uids: uidsInput.value
      })
    })
      .then(function (resp) {
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        return resp.json();
      })
      .then(function (data) {
        renderLeft(data);
        renderRight(data);
        renderMetrics(data);
        statusEl.textContent = "已更新 " + new Date().toLocaleTimeString();
      })
      .catch(function (err) {
        statusEl.textContent = "请求失败（页面不中断）: " + err.message;
      });
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(refresh, 150);
  }

  [modelInput, fieldsInput, uidsInput].forEach(function (el) {
    el.addEventListener("input", schedule);
  });

  document.getElementById("reset-btn").addEventListener("click", function () {
    fetch("/api/reset", { method: "POST" }).then(refresh);
  });

  refresh();
})();