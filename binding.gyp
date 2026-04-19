{
  "targets": [
    {
      "target_name": "delayed_delete",
      "sources": [
        "delayed_delete.cc"
      ],
      "include_dirs": [
        "<!@(node -p \"require('node-addon-api').include\")"
      ],
      "dependencies": [
        "<!(node -p \"require('node-addon-api').targets\"):node_addon_api"
      ],
      "defines": []
    }
  ]
}