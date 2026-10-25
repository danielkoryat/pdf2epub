// Test builds are signed with this computer's Android debug key, the key the earlier native Book Listen
// builds used, so the new app installs over them and keeps their books. A Play Store release needs its
// own upload key instead.
const { withDangerousMod } = require('expo/config-plugins');
const fs = require('fs');
const os = require('os');
const path = require('path');

module.exports = function withLocalDebugKey(config) {
  return withDangerousMod(config, [
    'android',
    (config) => {
      const key = path.join(os.homedir(), '.android', 'debug.keystore');
      const target = path.join(config.modRequest.platformProjectRoot, 'app', 'debug.keystore');
      if (fs.existsSync(key)) fs.copyFileSync(key, target);
      return config;
    },
  ]);
};
