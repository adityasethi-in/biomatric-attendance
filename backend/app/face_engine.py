import cv2
import numpy as np
from insightface.app import FaceAnalysis
import mediapipe as mp


class FaceEngine:
    def __init__(self):
        self.app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        self.app.prepare(ctx_id=0, det_size=(320, 320))
        self.mp_face_mesh = mp.solutions.face_mesh.FaceMesh(
            static_image_mode=False,
            max_num_faces=1,
            refine_landmarks=True,
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

    @staticmethod
    def decode_image(img_bytes: bytes):
        arr = np.frombuffer(img_bytes, np.uint8)
        return cv2.imdecode(arr, cv2.IMREAD_COLOR)

    def get_embedding(self, img):
        faces = self.app.get(img)
        if not faces:
            return None, None
        f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        emb = f.embedding.astype(np.float32)
        norm = np.linalg.norm(emb)
        if norm == 0:
            return None, None
        emb = emb / norm
        return emb.tolist(), float(f.det_score)

    def liveness_basic(self, img) -> bool:
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        res = self.mp_face_mesh.process(rgb)
        if not res.multi_face_landmarks:
            return False

        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        sharpness = cv2.Laplacian(gray, cv2.CV_64F).var()
        # Relaxed thresholds for laptop webcams (low light / motion blur tolerant).
        if sharpness < 15.0:
            return False

        h, w = gray.shape
        center = gray[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
        if center.size == 0:
            return False
        contrast = float(center.std())
        return contrast > 8.0
