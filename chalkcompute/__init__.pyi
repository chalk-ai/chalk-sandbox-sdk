from chalkcompute._connect import ConnectClient as ConnectClient
from chalkcompute._image import Image as Image
from chalkcompute._image import LazyLocalFile as LazyLocalFile
from chalkcompute._volume import VolumeClient as VolumeClient

def build_image(image: Image) -> str: ...
