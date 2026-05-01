# React Native Camera Core Logic

```tsx
import React, { useRef, useState } from "react";
import { Button, View, ActivityIndicator } from "react-native";
import { CameraView, useCameraPermissions } from "expo-camera";

export default function MarkAttendanceScreen() {
  const camRef = useRef<CameraView>(null);
  const [perm, requestPerm] = useCameraPermissions();
  const [loading, setLoading] = useState(false);

  if (!perm?.granted) return <Button title="Allow Camera" onPress={requestPerm} />;

  const markAttendance = async () => {
    if (!camRef.current || loading) return;
    setLoading(true);
    try {
      const photo = await camRef.current.takePictureAsync({ quality: 0.6, base64: false });
      const form = new FormData();
      form.append("image", { uri: photo.uri, type: "image/jpeg", name: "frame.jpg" } as any);

      const res = await fetch("http://<SERVER_IP>:7000/attendance/mark", {
        method: "POST",
        body: form,
        headers: { "Content-Type": "multipart/form-data" },
      });
      const data = await res.json();
      console.log(data);
    } finally {
      setLoading(false);
    }
  };

  return (
    <View style={{ flex: 1 }}>
      <CameraView ref={camRef} style={{ flex: 1 }} facing="front" />
      <Button title="Mark Attendance" onPress={markAttendance} />
      {loading && <ActivityIndicator />}
    </View>
  );
}
```