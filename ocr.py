import asyncio
import logging
from functools import lru_cache

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_reader():
    import easyocr
    logger.info("Загрузка модели EasyOCR (первый раз — занимает ~30 сек)...")
    return easyocr.Reader(["ru", "en"], gpu=False)


async def image_to_text(image_path: str) -> str:
    """Распознаёт текст на изображении. Возвращает строку."""
    loop = asyncio.get_event_loop()
    reader = _get_reader()
    results: list[str] = await loop.run_in_executor(
        None,
        lambda: reader.readtext(image_path, detail=False, paragraph=True),
    )
    return "\n".join(results)
