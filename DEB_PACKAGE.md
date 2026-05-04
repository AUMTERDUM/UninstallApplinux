# Debian Package Build

โปรเจกต์นี้สามารถ build เป็นแพ็กเกจ `.deb` ได้ทั้งแบบสคริปต์ตรง และแบบโครงสร้างมาตรฐาน Debian

## ไฟล์ที่ใช้

- `build_deb.sh` สำหรับสร้างไฟล์แพ็กเกจโดยตรง
- `debian/` สำหรับ build แบบ `dpkg-buildpackage`
- `linux-uninstaller` เป็น launcher command สำหรับแพ็กเกจระบบ
- `linux-uninstaller.desktop` เป็น launcher สำหรับเมนูแอปของแพ็กเกจระบบ
- `assets/linux-uninstaller.svg` เป็น icon ของแอป
- `linux_uninstaller.py` เป็นตัวแอปหลักที่จะถูกติดตั้งไปที่ `/opt/linux-uninstaller`

## วิธี build

รันคำสั่ง:

```bash
./build_deb.sh
```

ผลลัพธ์จะถูกสร้างไว้ที่:

```bash
dist/linux-uninstaller_<version>_all.deb
```

ถ้าไม่กำหนด `VERSION` เอง สคริปต์จะใช้ลำดับนี้:

- ค่า `VERSION` จาก environment ถ้ามี
- git tag ปัจจุบัน ถ้า checkout อยู่บน tag
- `1.0.0+git<hash>` ถ้าอยู่ใน git repo
- `1.0.0+local<date>` ถ้าไม่มี git metadata

ตัวอย่าง:

```bash
VERSION=1.2.0 ./build_deb.sh
```

## วิธี build แบบมาตรฐาน Debian

รันคำสั่ง:

```bash
dpkg-buildpackage -b -us -uc -d
```

ผลลัพธ์จะถูกสร้างในโฟลเดอร์แม่ของโปรเจกต์

## วิธีติดตั้ง

```bash
sudo dpkg -i dist/linux-uninstaller_<version>_all.deb
```

ถ้ามี dependency ขาด ให้ตามด้วย:

```bash
sudo apt-get install -f
```

หลังติดตั้งแล้วจะมี:

- ตัวโปรแกรมหลักที่ `/opt/linux-uninstaller/linux_uninstaller.py`
- คำสั่งเรียกแอปที่ `/usr/bin/linux-uninstaller`
- launcher ในเมนูแอปที่ `/usr/share/applications/linux-uninstaller.desktop`
- icon ที่ `/usr/share/icons/hicolor/scalable/apps/linux-uninstaller.svg`

## วิธีเปิดใช้งาน

- เปิดจากเมนูแอปด้วยชื่อ `Linux Uninstaller`
- หรือรันจาก terminal ด้วยคำสั่ง:

```bash
linux-uninstaller
```

## วิธีถอนการติดตั้ง

```bash
sudo dpkg -r linux-uninstaller
```

## หมายเหตุ

- แพ็กเกจนี้ใช้ `python3` จากระบบ และไม่ bundle virtual environment
- ตัวแอปจะ bind ที่ `127.0.0.1` เท่านั้น
- การถอนแอปจริงจากในโปรแกรมอาจเรียกสิทธิ์ผู้ดูแลระบบผ่าน `pkexec`