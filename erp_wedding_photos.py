"""Face detection and crop bounds shared with approved automatic scroll v21."""
from pathlib import Path
import math
ROOT=Path(__file__).with_name('wedding_scroll')

def detect_faces(image):
    import cv2
    import numpy as np

    model = ROOT/'assets'/'face_detection_yunet_2023mar.onnx'
    if not model.is_file():
        raise ValueError('얼굴 감지 모델 파일을 assets 폴더에 넣어 주세요.')
    preview = image.copy()
    preview.thumbnail((1280, 1280))
    detector = cv2.FaceDetectorYN.create(str(model), '', preview.size, 0.75, 0.3, 5000)
    _, detected = detector.detect(cv2.cvtColor(np.asarray(preview), cv2.COLOR_RGB2BGR))
    if detected is None or len(detected) != 2:
        return []
    sx, sy = image.width/preview.width, image.height/preview.height
    faces = []
    for row in detected:
        x,y,w,h = map(float, row[:4])
        faces.append((max(0,(x-.12*w)*sx), max(0,(y-.35*h)*sy),
                      min(image.width,(x+1.12*w)*sx), min(image.height,(y+1.15*h)*sy)))
    return faces


def face_crop_bounds(image_size, target_size, faces):
    width, height = image_size
    ratio = target_size[0] / target_size[1]
    crop_width = min(width, round(height * ratio))
    crop_height = min(height, round(width / ratio))
    bounds = []
    for axis, extent, crop_extent in zip((0, 1), image_size, (crop_width, crop_height)):
        start = min(face[axis] for face in faces)
        end = max(face[axis+2] for face in faces)
        low = max(0, math.ceil(end - crop_extent))
        high = min(extent - crop_extent, math.floor(start))
        if low > high:
            return None
        desired = round((start + end - crop_extent) / 2)
        offset = min(high, max(low, desired))
        bounds.append((offset, offset + crop_extent))
    return bounds[0][0], bounds[1][0], bounds[0][1], bounds[1][1]



def crop(image,size):
    from PIL import Image,ImageOps
    image=ImageOps.exif_transpose(image).convert('RGB')
    faces=detect_faces(image)
    if len(faces)!=2:raise ValueError('신랑신부 두 얼굴이 확인되는 사진을 선택하세요. 독사진이나 얼굴 확인이 어려운 사진은 제작에 사용하지 않습니다.')
    bounds=face_crop_bounds(image.size,size,faces)
    if bounds is None:raise ValueError('두 얼굴을 보존하면서 채울 수 없는 사진입니다. 다른 커플 사진을 선택하세요.')
    return image.crop(bounds).resize(size,Image.Resampling.LANCZOS)
