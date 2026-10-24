/* Styleguide-only behaviour: wires the demo buttons. Not part of the app. */
document.addEventListener('DOMContentLoaded', function () {
  var tones = { success: 'تغییرات ذخیره شد.', error: 'ذخیره انجام نشد؛ دوباره تلاش کنید.', warning: 'این مراسم با مراسم دیگری هم‌زمان است.', info: 'نیروی جدیدی به مراسم دعوت شد.' };
  Object.keys(tones).forEach(function (tone) {
    var btn = document.getElementById('sg-toast-' + tone);
    if (btn) btn.addEventListener('click', function () { Atelier.toast(tones[tone], tone); });
  });
  // the demo forms must not navigate away
  document.querySelectorAll('form[action="#"]').forEach(function (f) {
    f.addEventListener('submit', function (e) { e.preventDefault(); Atelier.toast('فرم ارسال می‌شد: ' + new URLSearchParams(new FormData(f)).toString().slice(0, 120), 'info'); });
  });
});
