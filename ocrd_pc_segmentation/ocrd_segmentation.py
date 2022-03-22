from __future__ import absolute_import

import json
import os.path

from pkg_resources import resource_string

import numpy as np
from ocr4all_pixel_classifier.lib.pc_segmentation import RectSegment
from ocrd import Processor
from ocrd_modelfactory import page_from_file
from ocrd_models.ocrd_page import (
    MetadataItemType,
    LabelsType, LabelType,
    TextRegionType,
    ImageRegionType,
    NoiseRegionType,
    CoordsType,
    to_xml,
)
from ocrd_utils import (
    assert_file_grp_cardinality,
    getLogger,
    make_file_id,
    MIMETYPE_PAGE,
)

OCRD_TOOL = json.loads(resource_string(__name__, 'ocrd-tool.json').decode('utf8'))
TOOL = 'ocrd-pc-segmentation'


def polygon_from_segment(segment: RectSegment):
    from ocrd_utils import polygon_from_bbox
    return polygon_from_bbox(segment.y_start, segment.x_start, segment.y_end, segment.x_end)


class PixelClassifierSegmentation(Processor):

    def __init__(self, *args, **kwargs):
        kwargs['ocrd_tool'] = OCRD_TOOL['tools'][TOOL]
        kwargs['version'] = OCRD_TOOL['version']
        super().__init__(*args, **kwargs)
        if hasattr(self, 'output_file_grp') and hasattr(self, 'parameter'):
            # processing context
            self.setup()

    def setup(self):
        LOG = getLogger('processor.PixelClassifierSegmentation')
        gpu_allow_growth = self.parameter['gpu_allow_growth']
        model = self.parameter['model']
        if model == '__DEFAULT__':
            from . import DEFAULT_SEGMENTATION_MODEL_PATH
            model = DEFAULT_SEGMENTATION_MODEL_PATH
        elif model == '__LEGACY__':
            from . import LEGACY_SEGMENTATION_MODEL_PATH
            model = LEGACY_SEGMENTATION_MODEL_PATH

        from ocr4all_pixel_classifier.lib.predictor import PredictSettings, Predictor
        from ocr4all.colors import ColorMap, DEFAULT_COLOR_MAPPING
        self.color_map = ColorMap(DEFAULT_COLOR_MAPPING)
        settings = PredictSettings(
            network=os.path.abspath(model),
            high_res_output=True,
            color_map=self.color_map,
            n_classes=len(DEFAULT_COLOR_MAPPING),
            gpu_allow_growth=gpu_allow_growth,
        )
        self.predictor = Predictor(settings)

    def process(self):
        """Performs segmentation on the input binary image

        Produces a PageXML file as output.
        """
        LOG = getLogger('processor.PixelClassifierSegmentation')
        assert_file_grp_cardinality(self.input_file_grp, 1)
        assert_file_grp_cardinality(self.output_file_grp, 1)

        overwrite_regions = self.parameter['overwrite_regions']
        xheight = self.parameter['xheight']
        resize_height = self.parameter['resize_height']

        for n, input_file in enumerate(self.input_files):
            page_id = input_file.pageId or input_file.ID
            LOG.info("INPUT FILE %i / %s", n, page_id)
            pcgts = page_from_file(self.workspace.download_file(input_file))
            self.add_metadata(pcgts)
            page = pcgts.get_Page()
            if page.get_TextRegion():
                if overwrite_regions:
                    LOG.info('removing existing TextRegions')
                    page.set_TextRegion([])
                else:
                    LOG.warning('keeping existing TextRegions')
            if page.get_ImageRegion():
                if overwrite_regions:
                    LOG.info('removing existing ImageRegions')
                    page.set_ImageRegion([])
                else:
                    LOG.warning('keeping existing ImageRegions')
            if overwrite_regions:
                page.set_AdvertRegion([])
                page.set_ChartRegion([])
                page.set_ChemRegion([])
                page.set_GraphicRegion([])
                page.set_LineDrawingRegion([])
                page.set_MathsRegion([])
                page.set_MusicRegion([])
                page.set_NoiseRegion([])
                page.set_SeparatorRegion([])
                page.set_TableRegion([])
                page.set_UnknownRegion([])

            page_image, page_coords, _ = self.workspace.image_from_page(page, page_id)

            # ensure the image doesn't have an alpha channel
            if page_image.mode[-1] == "A":
                page_image = page_image.convert(mode=page_image.mode[0:-1])
            page_binary = page_image.convert(mode='1')

            self._process_page(page, np.asarray(page_image), np.asarray(page_binary), page_coords, xheight, resize_height)

            file_id = make_file_id(input_file, self.output_file_grp)
            self.workspace.add_file(
                ID=file_id,
                file_grp=self.output_file_grp,
                pageId=input_file.pageId,
                mimetype=MIMETYPE_PAGE,
                local_filename=os.path.join(self.output_file_grp,
                                            file_id + '.xml'),
                content=to_xml(pcgts))

    def _process_page(self, page, page_image, page_binary, page_coords, xheight, resize_height):

        from ocr4all_pixel_classifier.lib.pc_segmentation import find_segments
        from ocr4all_pixel_classifier.lib.dataset import SingleData

        from ocr4all_pixel_classifier.lib.dataset import prepare_images
        image, binary = prepare_images(page_image, page_binary, target_line_height=8, line_height_px=xheight)

        data = SingleData(binary=binary, image=image, original_shape=binary.shape, line_height_px=xheight)

        masks = self.predictor.predict_masks(data)

        orig_height, orig_width = page_image.shape[0:2]
        mask_image = masks.inverted_overlay

        segments_text, segments_image = find_segments(orig_height, mask_image, xheight,
                                                      resize_height, self.color_map)

        def add_region(region: RectSegment, index: int, region_type: str):
            from ocrd_utils import coordinates_for_segment, points_from_polygon
            polygon = polygon_from_segment(region)
            polygon = coordinates_for_segment(polygon, page_image, page_coords)
            points = points_from_polygon(polygon)

            indexed_id = "region%04d" % index
            coords = CoordsType(points=points)
            if region_type == "text":
                page.add_TextRegion(TextRegionType(id=indexed_id, Coords=coords))
            elif region_type == "image":
                page.add_ImageRegion(ImageRegionType(id=indexed_id, Coords=coords))
            else:
                page.add_NoiseRegion(NoiseRegionType(id=indexed_id, Coords=coords))

        count = 0
        for r in segments_text:
            add_region(r, count, "text")
            count += 1
        for r in segments_image:
            add_region(r, count, "image")
            count += 1
