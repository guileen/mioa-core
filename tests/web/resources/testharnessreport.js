// Mioa runner adapter: keep upstream assertions unchanged; collect official results.
setup({output: false});
add_completion_callback(function(tests, status) {
  window.__mioa_wpt = {
    complete: true,
    harness: {status: status.status, message: status.message || ""},
    subtests: tests.map(function(t) {
      return {name: t.name, status: t.status, message: t.message || ""};
    })
  };
});
