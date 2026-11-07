const os = require("node:os");

os.userInfo = () => ({
  uid: -1,
  gid: -1,
  username: process.env.USERNAME || "user",
  homedir: process.env.USERPROFILE || "",
  shell: null,
});
