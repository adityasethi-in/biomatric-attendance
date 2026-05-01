export const CLIENT_FACE_MODEL = {
  name: "face-api-128",
  version: "vladmandic-face-api-1.7.15",
  dimension: 128,
};

const MODEL_URL = `${import.meta.env.BASE_URL || "/"}models/face-api`;
let modelPromise = null;
let faceApiPromise = null;

async function getFaceApi() {
  if (!faceApiPromise) {
    faceApiPromise = import("@vladmandic/face-api");
  }
  return faceApiPromise;
}

async function prepareBackend(faceapi) {
  try {
    await faceapi.tf.setBackend("webgl");
  } catch {
    try {
      await faceapi.tf.setBackend("wasm");
    } catch {
      await faceapi.tf.setBackend("cpu");
    }
  }
  await faceapi.tf.ready();
}

export async function loadClientFaceModel() {
  if (!modelPromise) {
    modelPromise = (async () => {
      const faceapi = await getFaceApi();
      await prepareBackend(faceapi);
      await Promise.all([
        faceapi.nets.tinyFaceDetector.loadFromUri(MODEL_URL),
        faceapi.nets.faceLandmark68TinyNet.loadFromUri(MODEL_URL),
        faceapi.nets.faceRecognitionNet.loadFromUri(MODEL_URL),
      ]);
      return true;
    })();
  }
  return modelPromise;
}

function descriptorToArray(descriptor) {
  return Array.from(descriptor).map((value) => Number(value));
}

async function blobToImage(blob) {
  const url = URL.createObjectURL(blob);
  try {
    const img = new Image();
    img.decoding = "async";
    img.src = url;
    await img.decode();
    return img;
  } finally {
    URL.revokeObjectURL(url);
  }
}

export async function getClientFaceDescriptor(input) {
  await loadClientFaceModel();
  const faceapi = await getFaceApi();
  const media = input instanceof Blob ? await blobToImage(input) : input;
  const options = new faceapi.TinyFaceDetectorOptions({
    inputSize: 320,
    scoreThreshold: 0.35,
  });
  const result = await faceapi
    .detectSingleFace(media, options)
    .withFaceLandmarks(true)
    .withFaceDescriptor();

  if (!result?.descriptor) {
    throw new Error("Client face model could not detect a clear face.");
  }

  return {
    embedding: descriptorToArray(result.descriptor),
    qualityScore: result.detection?.score || 1,
  };
}

export async function getClientFaceDescriptorsFromBlobs(blobs) {
  const embeddings = [];
  const qualityScores = [];
  for (const blob of blobs) {
    const descriptor = await getClientFaceDescriptor(blob);
    embeddings.push(descriptor.embedding);
    qualityScores.push(descriptor.qualityScore);
  }
  return { embeddings, qualityScores };
}
